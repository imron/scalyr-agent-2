FROM scalyr/scalyr-k8s-agent
ENV DEBIAN_FRONTEND noninteractive
MAINTAINER Scalyr Inc <support@scalyr.com>
RUN apt-get update && \
    apt-get install -y python-pip && \
    apt-get clean && \
    pip install yappi

CMD ["/usr/sbin/scalyr-agent-2", "--no-fork", "--no-change-user", "start"]
