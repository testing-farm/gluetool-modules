# Citool container with gluetool-modules-framework modules
#
# Volume mounts:
#    /var/ARTIFACTS     - volume mount for artifacts storage
#    /CONFIG            - place for configuration
#

# Based on CentOS 8 podman image, which makes it possible to run tmt container provisioner.
# https://github.com/thrix/podman/blob/main/contrib/podmanimage/stable/Dockerfile.centos8
FROM quay.io/testing-farm/podman:latest
LABEL maintainer="tft@redhat.com"

# workdir needs to be a writable path in all supported operating systems, a tmt requirement
WORKDIR /var/ARTIFACTS

# PycURL needs to be compiled against openssl to work properly
ENV PYCURL_SSL_BACKEND=openssl

# Disable gluetool tracing, we do not use it currently
ENV GLUETOOL_TRACING_DISABLE=1

# Enable SSH pipelinening in Ansible
ENV ANSIBLE_PIPELINING=1

# Virtual environment bin path
ENV PATH="/opt/gluetool/bin:$PATH"

# Ansible requirements
COPY container/requirements.yml /etc/ansible/requirements.yml

# install all required dependencies
RUN    ln -s /CONFIG /etc/citool.d \
    && rpm -Uvh https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
    && dnf -y update \
    && dnf -y install ansible-core autoconf automake crun gcc git krb5-devel libcurl-devel \
                      libpq-devel libtool libxml2-devel make openssl-devel popt-devel \
                      postgresql-devel python39 python39-devel python39-pip python3-libselinux \
                      redhat-rpm-config koji python3-virtualenv \
                      rsync beakerlib-libraries rpm-build rpm-devel \
                      tmt tmt-provision-container tmt-provision-virtual tmt-report-polarion tmt-report-html \
    # workaround for https://pagure.io/standard-test-roles/pull-request/410
    && dnf copr enable -y @osci/standard-test-roles \
    && dnf -y install standard-test-roles \
    # install tmt from updates-testing, an agreement with tmt folks to be able to test tmt releases sooner.
    # && dnf -y update --enablerepo=epel-testing tmt-provision-container tmt-provision-virtual tmt \
    #                                           tmt-report-polarion tmt-report-html \
    && dnf clean all \
    # Install pylero from PyPI, it is broken in CentOS 8
    && rpm -e --nodeps python3-suds \
    && pip3.6 install -t /lib/python3.6/site-packages pylero \
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
