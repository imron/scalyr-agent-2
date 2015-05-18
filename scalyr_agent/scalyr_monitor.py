# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# Contains the base class for all monitor plugins used by the Scalyr agent.
# This class should be used by developers creating their own monitor plugins.
#
# To see how to write your own Scalyr monitor plugin, please see:
# https://www.scalyr.com/help/creating-a-monitor-plugin
#
# author: Steven Czerwinski <czerwin@scalyr.com>
import inspect
import os
import sys

__author__ = 'czerwin@scalyr.com'

from threading import Lock

import scalyr_agent.scalyr_logging as scalyr_logging

from scalyr_agent.util import StoppableThread

log = scalyr_logging.getLogger(__name__)


class ScalyrMonitor(StoppableThread):
    """The default number of seconds between gathering a sample.  This is the global default, which should
    be set from the configuration file.
    """
    DEFAULT_SAMPLE_INTERVAL_SECS = 30.0

    """The base class for all monitors used by the agent.

    An instance of a monitor will be created for every reference to this module in the "monitors"
    section in the agent.json configuration file.  The monitor will be
    executed in its own thread and will be expected to send all output to
    provided Loggers.  Do not used stdout, stderr.

    Public attributes:  (must be updated in derived constructor)
        log_config:  A dict containing the log configuration object that
            should be used to configure the agent to copy the log generated
            by this module.  It has the same format as the entries in the
            "logs" section in agent.json.  In particular, the module
            can use this to specify the path of the log file where all emitted metric values
            from this monitor will be sent (using self._logger), set attributes to
            associate with all log lines generated by the module, specify a parser
            for the log, as well as set sampling rules.

            Note, if the "path" field in "log_config" is not absolute, it will be resolved relative to the
            directory specified by the "agent_log_path" option in the configuration file.
        disabled:  A boolean indicating if this module instance should be
            run.
    """
    def __init__(self, monitor_config, logger, sample_interval_secs=None):
        """Constructs an instance of the monitor.

        It is optional for derived classes to override this method.  The can instead
        override _initialize which is invoked during initialization.
        TODO:  Determine which approach is preferred by developers and recommend that.

        If a derived class overrides __init__, they must invoke this method in the override method.

        This method will set default values for all public attributes (log_config, disabled, etc).  These
        may be overwritten by the derived class.

        The derived classes must raise an Exception (or something derived from Exception)
        in this method if the provided configuration is invalid or if there is any other
        error known at this time preventing the module from running.

        @param monitor_config: A dict containing the configuration information for this module instance from the
            configuration file. The only valid values are strings, ints, longs, floats, and booleans.
        @param logger: The logger to use for output.
        @param sample_interval_secs: The interval in seconds to wait between gathering samples.  If None, it will
            set the value from the ``sample_interval`` field in the monitor_config if present, or the default
            interval time for all monitors in ``DEFAULT_SAMPLE_INTERVAL_SECS``.  Generally, you should probably
            pass None here and allow the value to be taken from the configuration files.
        """
        # The logger instance that this monitor should use to report all information and metric values.
        self._logger = logger
        self.monitor_name = monitor_config['module']
        # The MonitorConfig object created from the config for this monitor instance.
        self._config = MonitorConfig(monitor_config, monitor_module=self.monitor_name)
        log_path = self.monitor_name.split('.')[-1] + '.log'
        self.disabled = False
        # TODO: For now, just leverage the logic in the loggers for naming this monitor.  However,
        # we should have it be more dynamic where the monitor can override it.
        if logger.component.find('monitor:') == 0:
            self.monitor_name = logger.component[8:]
        else:
            self.monitor_name = logger.component
        self.log_config = {
            "path": log_path,
        }
        # This lock protects all variables that can be access by other threads, reported_lines,
        # emitted_lines, and errors.  It does not protect _run_state since that already has its own lock.
        self.__lock = Lock()
        self.__reported_lines = 0
        self.__errors = 0

        # Set the time between samples for this monitor.  We prefer configuration values over the values
        # passed into the constructor.
        if sample_interval_secs is not None:
            self._sample_interval_secs = sample_interval_secs
        else:
            self._sample_interval_secs = self._config.get('sample_interval', convert_to=float,
                                                          default=ScalyrMonitor.DEFAULT_SAMPLE_INTERVAL_SECS)

        self.__metric_log_open = False

        # These variables control the rate limiter on how fast we can write to the metric log.
        # The first one is the average number of bytes that can be written per second.  This is the bucket fill rate
        # in the "leaky bucket" algorithm used to calculate the rate limit.  Derived classes may change this.
        self._log_write_rate = 2000
        # This is the maximum size of a write burst to the log.  This is the bucket size in the "leaky bucket" algorithm
        # used to calculate the rate limit.  Derived classes may change this.
        self._log_max_write_burst = 100000

        self._initialize()

        StoppableThread.__init__(self, name='metric thread')

    def _initialize(self):
        """Can be overridden by derived classes to perform initialization functions before the monitor is run.

        This is meant to allow derived monitors to perform some initialization and configuration validation
        without having to override the __init__ method (and be responsible for passing all of the arguments
        to the super class).

        The derived classes must raise an Exception (or something derived from Exception)
        in this method if the provided configuration is invalid or if there is any other
        error known at this time preventing the module from running.

        NOTE: This will be called everytime the agent script is run, including when *stopping* the agent.
        Therefore it is not the best place to do things like create sockets/open files etc.
        """
        pass

    @property
    def module_name(self):
        """Returns the name of the module that will run this monitor.
        """
        return self._config['module']

    def reported_lines(self):
        """Returns the number of metric lines emitted to the metric log for this monitor.

        This is calculated by counting how many times the logger instance on this monitor's report_values
        method was invoked and all the times any logger has logged anything with metric_log_for_monitor set
        to this monitor.
        """
        self.__lock.acquire()
        result = self.__reported_lines
        self.__lock.release()
        return result

    def errors(self):
        """Returns the number of errors experienced by the monitor as it is running.

        For monitors just implementing the gather_sample method, this will be the number of times
        that invocation raised an exception.  If a monitor overrides the run method, then it is up to
        them to increment the errors as appropriate using increment_counter.
        """
        self.__lock.acquire()
        result = self.__errors
        self.__lock.release()
        return result

    def increment_counter(self, reported_lines=0, errors=0):
        """Increment some of the counters pertaining to the performance of this monitor.
        """
        self.__lock.acquire()
        self.__reported_lines += reported_lines
        self.__errors += errors
        self.__lock.release()

    def run(self):
        """Begins executing the monitor, writing metric output to logger.

        Implements the business logic for this monitor.  This method will
        be invoked by the agent on its own thread.  This method should
        only return if the monitor instance should no longer be executed or
        if the agent is shutting down.

        The default implementation of this method will invoke the
        "gather_sample" once every sample_period time, emitting the returned
        dict to logger.  Derived classes may override this method if they
        wish.

        This method should use "self._logger" to report any output.  It should use
        "self._logger.emit_value" to report any metric values generated by the monitor
        plugin.  See the documentation for 'scalyr_logging.AgentLogger.emit_value' method for more details.
        """
        # noinspection PyBroadException
        try:
            while not self._is_stopped():
                # noinspection PyBroadException
                try:
                    self.gather_sample()
                except Exception:
                    self._logger.exception('Failed to gather sample due to the following exception')
                    self.increment_counter(errors=1)

                self._sleep_but_awaken_if_stopped(self._sample_interval_secs)
            self._logger.info('Monitor has finished')
        except Exception:
            # TODO:  Maybe remove this catch here and let the higher layer catch it.  However, we do not
            # right now join on the monitor threads, so no one would catch it.  We should change that.
            self._logger.exception('Monitor died from due to exception:', error_code='failedMonitor')

    def gather_sample(self):
        """Derived classes should implement this method to gather a data sample for this monitor plugin
        and report it.

        If the default "run" method implementation is not overridden, then
        derived classes must implement this method to actual perform the
        monitor-specific work to gather whatever information it should be
        collecting.

        It is expected that the derived class will report any gathered metric samples
        by using the 'emit_value' method on self._logger.  They may invoke that method
        multiple times in a single 'gather_sample' call to report multiple metrics.
        See the documentation for 'scalyr_logging.AgentLogger.emit_value' method for more details.

        Any exceptions raised by this method will be reported as an error but will
        not stop execution of the monitor.
        """
        pass

    def set_sample_interval(self, secs):
        """Sets the number of seconds between calls to gather_sample when running.

        This must be invoked before the monitor is started.

        @param secs: The number of seconds, which can be fractional.
        """
        self._sample_interval_secs = secs

    def open_metric_log(self):
        """Opens the logger for this monitor.

        This must be invoked before the monitor is started."""
        self._logger.openMetricLogForMonitor(self.log_config['path'], self, max_write_burst=self._log_max_write_burst,
                                             log_write_rate=self._log_write_rate)
        self.__metric_log_open = True
        return True

    def close_metric_log(self):
        """Closes the logger for this monitor.

        This must be invoked after the monitor has been stopped."""
        if self.__metric_log_open:
            self._logger.closeMetricLog()
            self.__metric_log_open = False

    def _is_stopped(self):
        """Returns whether or not the "stop" method has been invoked."""
        return not self._run_state.is_running()

    def _sleep_but_awaken_if_stopped(self, time_to_sleep):
        """Sleeps for the specified amount of seconds or until the stop() method on this instance is invoked, whichever
         comes first.

        @param time_to_sleep: The number of seconds to sleep.

        @return: True if the stop() has been invoked.
        """
        return self._run_state.sleep_but_awaken_if_stopped(time_to_sleep)


def load_monitor_class(module_name, additional_python_paths):
    """Loads the ScalyrMonitor class from the specified module and return it.

    This examines the module, locates the first class derived from ScalyrMonitor (there should only be one),
    and returns it.

    @param module_name: The name of the module
    @param additional_python_paths: A list of paths (separate by os.pathsep) to add to the PYTHONPATH when
        instantiating the module in case it needs to read other packages.

    @type module_name: str
    @type additional_python_paths: str

    @return: A tuple containing the class for the monitor and the MonitorInformation object for it.
    @rtype: (class, MonitorInformation)
    """

    original_path = list(sys.path)

    # Add in the additional paths.
    if additional_python_paths is not None and len(additional_python_paths) > 0:
        for x in additional_python_paths.split(os.pathsep):
            sys.path.append(x)

    MonitorInformation.set_monitor_info(module_name)
    # Load monitor.
    try:
        module = __import__(module_name)
        # If this a package name (contains periods) then we have to walk down
        # the subparts to get the actual module we wanted.
        for n in module_name.split(".")[1:]:
            module = getattr(module, n)

        # Now find any class that derives from ScalyrMonitor
        for attr in module.__dict__:
            value = getattr(module, attr)
            if not inspect.isclass(value):
                continue
            if 'ScalyrMonitor' in str(value.__bases__):
                MonitorInformation.set_monitor_info(module_name, description=value.__doc__)
                return value, MonitorInformation.get_monitor_info(module_name)

        return None, None
    finally:
        # Be sure to reset the PYTHONPATH
        sys.path = original_path


def define_config_option(monitor_module, option_name, option_description, required_option=False,
                         max_value=None, min_value=None, convert_to=None, default=None):
    """Defines a configuration option for the specified monitor.

    Once this is invoked, any validation rules supplied here are applied to all MonitorConfig objects created
    with the same monitor name.

    Note, this overwrites any previously defined rules for this configuration option.

    @param monitor_module:  The module the monitor is defined in.  This must be the same name that will be supplied
        for any MonitorConfig instances for this monitor.
    @param option_name: The name of the option field.
    @param required_option: If True, then this is option considered to be required and when the configuration
        is parsed for the monitor, a BadMonitorConfiguration exception if the field is not present.
    @param convert_to: If not None, then will convert the value for the option to the specified type. Only int,
        bool, float, long, str, and unicode are supported. If the type conversion cannot be done, a
        BadMonitorConfiguration exception is raised during configuration parsing. The only true conversions allowed are
        those from str, unicode value to other types such as int, bool, long, float. Trivial conversions are allowed
        from int, long to float, but not the other way around. Additionally, any primitive type can be converted to
        str, unicode.
    @param default: The value to assign to the option if the option is not present in the configuration. This is
        ignored if 'required_option' is True.
    @param max_value: If not None, the maximum allowed value for option. Raises a BadMonitorConfiguration if the
        value is greater during configuration parsing.
    @param min_value: If not None, the minimum allowed value for option. Raises a BadMonitorConfiguration if the
        value is less than during configuration parsing.
    """
    option = ConfigOption()
    option.option_name = option_name
    option.description = option_description
    option.required_option = required_option
    option.max_value = max_value
    option.min_value = min_value
    option.convert_to = convert_to
    option.default = default

    MonitorInformation.set_monitor_info(monitor_module, option=option)

    return None


def define_metric(monitor_module, metric_name, description, extra_fields=None, unit=None, cumulative=False,
                  category=None):
    """Defines description information for a metric with the specified name and extra fields.

    This will overwrite previous metric information recorded for the same ``metric_name`` and ``extra_fields``.

    Currently, this information is only used when creating documentation pages for the monitor.  Not all of the fields
    are used but will be used in the future.

    @param monitor_module:  The module name for the monitor this metric is defined in.
    @param metric_name: The name of the metric.
    @param description: The description of the metric.
    @param extra_fields: A dict describing the extra fields that are recorded with this metric.  It maps from the
        extra field name to the values that the description apply to.
    @param unit: A string describing the units of the value.  For now, this should be 'sec' or 'bytes'.  You may also
        include a colon after the unit with a scale factor.  For example, 'sec:.01' indicates the value represents
        1/100ths of a second.  You may also specify 'milliseconds', which is mapped to 'sec:.001'
    @param cumulative: True if the metric records the sum all metric since the monitored process began.  For example,
        it could be the sum of all request sizes received by a server.  In this case, calculating the difference between
        two values for the metric is the same as calculating the rate of non-accumulated metric.
    @param category: The category of the metric.  Each category will get its own table when printing the documentation.
        This should be used when there are many metrics and they need to be broken down into smaller groups.

    @type monitor_module: str
    @type metric_name: str
    @type description: str
    @type extra_fields: dict
    @type unit: str
    @type cumulative: bool
    @type category: str
    """

    info = MetricDescription()
    info.metric_name = metric_name
    info.description = description
    info.extra_fields = extra_fields
    info.unit = unit
    info.cumulative = cumulative
    info.category = category
    MonitorInformation.set_monitor_info(monitor_module, metric=info)


def define_log_field(monitor_module, field_name, field_description):
    """Defines a field that can be parsed from the log lines generated by the specified monitor.

    Note, this overwrites any previously defined rules for this log field.

    @param monitor_module:  The module the monitor is defined in.  This must be the same name that will be supplied
        for any MonitorConfig instances for this monitor.
    @param field_name: The name of the log field.
    @param field_description: The description for the log field.

    """
    log_field = LogFieldDescription()
    log_field.field = field_name
    log_field.description = field_description

    MonitorInformation.set_monitor_info(monitor_module, log_field=log_field)

    return None


class MonitorInformation(object):
    """Encapsulates all the descriptive information that can be gather for a particular monitor.

    This is generally used to create documentation pages for the monitor.
    """
    def __init__(self, monitor_module):
        self.__monitor_module = monitor_module
        self.__description = None
        # Maps from option name to the ConfigOption object that describes it.
        self.__options = {}
        # Maps from metric name with extra fields to the MetricDescription object that describes it.
        self.__metrics = {}
        # Maps from log field name to the LogFieldDescription object that describes it.
        self.__log_fields = {}
        # A counter used to determine insert sort order.
        self.__counter = 0

    @property
    def monitor_module(self):
        """Returns the module the monitor is defined in.

        @return: The module the monitor is defined in.
        @rtype: str
        """
        return self.__monitor_module

    @property
    def description(self):
        """Returns a description for the monitor using markdown.

        @return: The description
        @rtype: str
        """
        return self.__description

    @property
    def config_options(self):
        """Returns the configuration options for this monitor.

        @return: A list of the options
        @rtype: list of ConfigOption
        """
        return sorted(self.__options.itervalues(), key=self.__get_insert_sort_position)

    @property
    def metrics(self):
        """Returns descriptions for the metrics recorded by this monitor.

        @return: A list of metric descriptions
        @rtype: list of MetricDescription
        """
        return sorted(self.__metrics.itervalues(), key=self.__get_insert_sort_position)

    @property
    def log_fields(self):
        """Returns the log fields that are parsed from the log lines generated by this monitor.

        @return: A list of the log fields.
        @rtype: list of LogFieldDescription
        """
        return sorted(self.__log_fields.itervalues(), key=self.__get_insert_sort_position)

    def __get_insert_sort_position(self, item):
        """Returns the key to use for sorting the item by its insert position.

        This relies on the 'sort_pos' attribute added to all ConfigOption, MetricDescription, and
        LogFieldDescription objects when added to a monitor's information.

        @param item: The object
        @type item: object

        @return: The insert position of the item
        @rtype: int
        """
        return getattr(item, 'sort_pos')

    __monitor_info__ = {}

    @staticmethod
    def set_monitor_info(monitor_module, description=None, option=None, metric=None, log_field=None):
        """Sets information for the specified monitor.

        @param monitor_module: The module the monitor is defined in.
        @param description: If not None, sets the description for the monitor, using markdown.
        @param option: If not None, adds the specified configuration option to the monitor's information.
        @param metric: If not None, adds the specified metric description to the monitor's information.
        @param log_field: If not None, adds the specified log field description to the monitor's information.

        @type monitor_module: str
        @type description: str
        @type option: ConfigOption
        @type metric: MetricDescription
        @type log_field: LogFieldDescription
        """
        if monitor_module not in MonitorInformation.__monitor_info__:
            MonitorInformation.__monitor_info__[monitor_module] = MonitorInformation(monitor_module)

        info = MonitorInformation.__monitor_info__[monitor_module]
        if description is not None:
            info.__description = description

        # Increment the counter we use to recorder insert order.
        info.__counter += 1

        if option is not None:
            info.__options[option.option_name] = option
            # Stash a position attribute to capture what the insert order was for the options.
            setattr(option, 'sort_pos', info.__counter)

        if metric is not None:
            if metric.extra_fields is None:
                info.__metrics[metric.metric_name] = metric
            else:
                # If there are extra fields, we use that as part of the key name to store the metric under to
                # avoid collisions with the same metric but different extra fields registered.
                info.__metrics['%s%s' % (metric.metric_name, str(metric.extra_fields))] = metric
            # Stash a position attribute to capture what the insert order was for the metrics.
            setattr(metric, 'sort_pos', info.__counter)

        if log_field is not None:
            info.__log_fields[log_field.field] = log_field
            # Stash a position attribute to capture what the insert order was for the log fields.
            setattr(log_field, 'sort_pos', info.__counter)

    @staticmethod
    def get_monitor_info(monitor_module):
        """Returns the MonitorInformation object for the monitor defined in ``monitor_module``.

        @param monitor_module: The module the monitor is defined in.
        @type monitor_module: str

        @return: The information for the specified monitor, or none if it has not been loaded.
        @rtype: MonitorInformation
        """
        if monitor_module in MonitorInformation.__monitor_info__:
            return MonitorInformation.__monitor_info__[monitor_module]
        else:
            return None


class ConfigOption(object):
    """Simple object to hold the fields for a single configuration option.
    """
    def __init__(self):
        # The name of the option.
        self.option_name = None
        # The description of the option.
        self.description = None
        # True if the option is required.
        self.required_option = False
        # The maximum value allowed value for the option if any.
        self.max_value = None
        # The minimum value allowed value for the option if any.
        self.min_value = None
        # The primitive type to convert the value to.
        self.convert_to = None
        # The default value, if any.
        self.default = None


class MetricDescription(object):
    """Simple object to hold fields describing a monitor's metric."""
    def __init__(self):
        # The name of the metric.
        self.metric_name = None
        # The description for the metric.
        self.description = None
        # A dict containing a map of the extra fields included in the metric along with the format for the values.
        self.extra_fields = None
        # A string describing the units of the value.  For now, this should be 'sec' or 'bytes'.  You may also include
        # a colon after the unit with a scale factor.  For example, 'sec:.01' indicates the value represents 1/100ths
        # of a second.  You may also specify 'milliseconds', which is mapped to 'sec:.001'.
        self.unit = None
        # True if the metric records the sum all metric since the monitored process began.  For example, it could be
        # the sum of all the latencies for all requested received by the server.
        self.cumulative = False
        # The category for this metric.  This needs only to be supplied if the metric list is long for a particular
        # monitor.
        self.category = None


class LogFieldDescription(object):
    """Simple object to hold fields describing the entries that are parsed from a log line produced by the monitor."""
    def __init__(self):
        # The name of the field in the log line.
        self.field = None
        # The meaning of the field.
        self.description = None


class MonitorConfig(object):
    """Encapsulates configuration parameters for a single monitor instance and includes helper utilities to
    validate configuration values.

    This supports most of the operators and methods that dict supports, but has additional support to allow
    Monitor developers to easily validate configuration values.  See the get method for more details.

    This abstraction does not support any mutator operations.  The configuration is read-only.
    """
    def __init__(self, content=None, monitor_module=None):
        """Initializes MonitorConfig.

        @param content: A dict containing the key/values pairs to use.
        @param monitor_module: The module containing the monitor.  This must be the same as what was previously
            used for 'define_config_option' for any options registered for this monitor.
        """
        self.__map = {}
        if content is not None:
            for x in content:
                self.__map[x] = content[x]

        info = MonitorInformation.get_monitor_info(monitor_module)
        if info is not None:
            for x in info.config_options:
                if x.required_option or x.default is not None or x.option_name in self.__map:
                    self.__map[x.option_name] = self.get(x.option_name, required_field=x.required_option,
                                                         max_value=x.max_value, min_value=x.min_value,
                                                         convert_to=x.convert_to, default=x.default)

    def __len__(self):
        """Returns the number of keys in the JsonObject"""
        return len(self.__map)

    def get(self, field, required_field=False, max_value=None, min_value=None,
            convert_to=None, default=None):
        """Returns the value for the requested field.

        This method will optionally apply some validation rules as indicated by the optional arguments.  If any
        of these validation operations fail, then a BadMonitorConfiguration exception is raised.  Monitor developers are
        encouraged to catch this exception at their layer.

        @param field: The name of the field.
        @param required_field: If True, then will raise a BadMonitorConfiguration exception if the field is not
            present.
        @param convert_to: If not None, then will convert the value for the field to the specified type. Only int,
            bool, float, long, str, and unicode are supported. If the type conversion cannot be done, a
            BadMonitorConfiguration exception is raised. The only true conversions allowed are those from str, unicode
            value to other types such as int, bool, long, float. Trivial conversions are allowed from int, long to
            float, but not the other way around. Additionally, any primitive type can be converted to str, unicode.
        @param default: The value to return if the field is not present in the configuration. This is ignored if
            'required_field' is True.
        @param max_value: If not None, the maximum allowed value for field. Raises a BadMonitorConfiguration if the
            value is greater.
        @param min_value: If not None, the minimum allowed value for field. Raises a BadMonitorConfiguration if the
            value is less than.

        @return: The value
        @raise BadMonitorConfiguration: If any of the conversion or required rules are violated.
        """
        if required_field and field not in self.__map:
            raise BadMonitorConfiguration('Missing required field "%s"' % field, field)
        result = self.__map.get(field, default)

        if result is None:
            return result

        if convert_to is not None and type(result) != convert_to:
            result = self.__perform_conversion(field, result, convert_to)

        if max_value is not None and result > max_value:
            raise BadMonitorConfiguration('Value of %s in field "%s" is invalid; maximum is %s' % (
                                          str(result), field, str(max_value)), field)

        if min_value is not None and result < min_value:
            raise BadMonitorConfiguration('Value of %s in field "%s" is invalid; minimum is %s' % (
                                          str(result), field, str(min_value)), field)

        return result

    def __perform_conversion(self, field_name, value, convert_to):
        value_type = type(value)
        primitive_types = (int, long, float, str, unicode, bool)
        if convert_to not in primitive_types:
            raise Exception('Unsupported type for conversion passed as convert_to: "%s"' % str(convert_to))
        if value_type not in primitive_types:
            raise BadMonitorConfiguration('Unable to convert type %s for field "%s" to type %s' % (
                str(value_type), field_name, str(convert_to)), field_name)

        # Anything is allowed to go to str/unicode
        if convert_to == str or convert_to == unicode:
            return convert_to(value)

        # Anything is allowed to go from string/unicode to the conversion type, as long as it can be parsed.
        # Handle bool first.
        if value_type in (str, unicode):
            if convert_to == bool:
                return str(value).lower() == 'true'
            elif convert_to in (int, float, long):
                try:
                    return convert_to(value)
                except ValueError:
                    raise BadMonitorConfiguration('Could not parse value %s for field "%s" as numeric type %s' % (
                                                  value, field_name, str(convert_to)), field_name)

        if convert_to == bool:
            raise BadMonitorConfiguration('A numeric value %s was given for boolean field "%s"' % (
                                          str(value), field_name), field_name)

        # At this point, we are trying to convert a number to another number type.  We only allow long to int,
        # and long, int to float.
        if convert_to == float and value_type in (long, int):
            return float(value)
        if convert_to == long and value_type == int:
            return long(value)

        raise BadMonitorConfiguration('A numeric value of %s was given for field "%s" but a %s is required.', (
                                      str(value), field_name, str(convert_to)))

    def __iter__(self):
        return self.__map.iterkeys()

    def iteritems(self):
        """Returns an iterator over the items (key/value tuple) for this object."""
        return self.__map.iteritems()

    def itervalues(self):
        """Returns an iterator over the values for this object."""
        return self.__map.itervalues()

    def iterkeys(self):
        """Returns an iterator over the keys for this object."""
        return self.__map.iterkeys()

    def items(self):
        """Returns a list of items (key/value tuple) for this object."""
        return self.__map.items()

    def values(self):
        """Returns a list of values for this object."""
        return self.__map.values()

    def keys(self):
        """Returns a list keys for this object."""
        return self.__map.keys()

    def __getitem__(self, field):
        if not field in self:
            raise KeyError('The missing field "%s" in monitor config.' % field)
        return self.__map[field]

    def copy(self):
        result = MonitorConfig()
        result.__map = self.__map.copy()
        return result

    def __contains__(self, key):
        """Returns True if the JsonObject contains a value for key."""
        return key in self.__map

    def __eq__(self, other):
        if other is None:
            return False
        if type(self) is not type(other):
            return False
        assert isinstance(other.__map, dict)
        return self.__map == other.__map

    def __ne__(self, other):
        return not self.__eq__(other)


class BadMonitorConfiguration(Exception):
    """Exception indicating a bad monitor configuration, such as missing a required field."""
    def __init__(self, message, field):
        self.field = field
        Exception.__init__(self, message)


class UnsupportedSystem(Exception):
    """Exception indicating a particular monitor is not supported on this system."""
    def __init__(self, monitor_name, message):
        """Constructs an instance of the exception.

        @param monitor_name: The name of the monitor
        @param message: A message indicating what require was violated, such as requires Python version 2.6 or greater.
        """
        Exception.__init__(self, message)
        self.monitor_name = monitor_name
