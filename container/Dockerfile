# Citool container with gluetool-modules-framework modules
#
# Volume mounts:
#    /var/ARTIFACTS     - volume mount for artifacts storage
#    /CONFIG            - place for configuration
#

FROM quay.io/containers/podman:v5.1.1
LABEL maintainer="tft@redhat.com"

# workdir needs to be a writable path in all supported operating systems, a tmt requirement
WORKDIR /var/ARTIFACTS

# PycURL needs to be compiled against openssl to work properly
ENV PYCURL_SSL_BACKEND=openssl

# Disable gluetool tracing, we do not use it currently
ENV GLUETOOL_TRACING_DISABLE=1

# Enable SSH pipelinening in Ansible
ENV ANSIBLE_PIPELINING=1
ENV ANSIBLE_SSH_PIPELINING=1

# Make sure python does not buffer output
ENV PYTHONUNBUFFERED=1

# Virtual environment bin path
ENV PATH="/opt/gluetool/bin:$PATH"

# Supress cgroups v1 warning in Amazon Linux 2
# Note that podman v6.x will drop cgroup v1 support
ENV PODMAN_IGNORE_CGROUPSV1_WARNING=1

# Ansible requirements
COPY container/requirements.yml /etc/ansible/requirements.yml

# ~/.gitconfig is required for some operations of the git library in gluetool-modules
COPY container/.gitconfig /root/.gitconfig

# make sure we have latest packages installed
RUN dnf -y update

# install all required dependencies
RUN    ln -s /CONFIG /etc/citool.d \
    && dnf -y install ansible-core autoconf automake crun gcc git krb5-devel libcurl-devel \
                      libpq-devel libtool libxml2-devel make openssl-devel popt-devel \
                      postgresql-devel python3.9 python3-libselinux \
                      redhat-rpm-config koji python3-virtualenv standard-test-roles \
                      rsync beakerlib-libraries rpm-build rpm-devel awscli \
    && dnf copr -y enable @teemtee/tmt \
    # dropped version to pick latest pre-release
    && dnf -y install tmt-1.34.0 \
                      tmt+provision-container-1.34.0 \
                      tmt+provision-virtual-1.34.0 \
                      tmt+report-polarion-1.34.0 \
    # common SSH configuration for testing
    && bash -c "echo 'Host *' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  StrictHostKeyChecking no' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  UserKnownHostsFile /dev/null' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  SendEnv -LC_* -LANG_*' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  ServerAliveInterval 60' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  ServerAliveCountMax 5' >> /etc/ssh/ssh_config"

# copy the built package
COPY dist/gluetool_modules_framework*.whl .

# install gluetool-modules-framework with citool and required ansible collections
RUN    virtualenv -p python3.9 /opt/gluetool \
    && source /opt/gluetool/bin/activate \
    && pip3.9 install gluetool_modules_framework*.whl \
    && ansible-galaxy install --timeout 120 -r /etc/ansible/requirements.yml \
    && rm -f gluetool_modules_framework*.whl \
    && rm -rf ~/.cache

# run citool as entrypoint
ENTRYPOINT ["citool"]
CMD ["--help"]
