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

# Virtual environment bin path
ENV PATH="/opt/gluetool/bin:$PATH"

# install all required dependencies
# WORKAROUNDS:
#   * ansible-core is incompatible with standard-test-roles, so ansible-2.9.27 from centos-release-ansible-29
RUN    ln -s /CONFIG /etc/citool.d \
    && rpm -Uvh https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
    && dnf -y update \
    && dnf install -y centos-release-ansible-29 \
    && dnf install -y --allowerasing ansible-2.9.27 \
    && dnf -y install autoconf automake crun gcc git krb5-devel libcurl-devel \
                      libpq-devel libtool libxml2-devel make openssl-devel popt-devel \
                      postgresql-devel python39 python39-devel python39-pip python3-libselinux \
                      redhat-rpm-config standard-test-roles koji python3-virtualenv \
                      rsync beakerlib-libraries rpm-build rpm-devel \
                      tmt-provision-container tmt-provision-virtual tmt-report-polarion tmt-report-html \
    # install tmt from updates-testing, an agreement with tmt folks to be able to test tmt releases sooner.
    && dnf -y update --enablerepo=epel-testing tmt-provision-container tmt-provision-virtual tmt \
                                               tmt-report-polarion tmt-report-html \
    && dnf clean all \
    # TFT-1503 - install pylero from PyPI, not yet available as a package
    && pip3.9 install -t /usr/lib/python3.9/site-packages pylero \
    # common SSH configuration for testing
    && bash -c "echo 'Host *' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  StrictHostKeyChecking no' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  UserKnownHostsFile /dev/null' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  SendEnv -LC_* -LANG_*' >> /etc/ssh/ssh_config"

# copy the built package
COPY dist/gluetool_modules_framework*.whl .

# install gluetool-modules-framework & citool
RUN    virtualenv -p python3.9 /opt/gluetool \
    && source /opt/gluetool/bin/activate \
    && pip3.9 install gluetool_modules_framework*.whl \
    && rm -f gluetool_modules_framework*.whl \
    && rm -rf ~/.cache

# run citool as entrypoint
ENTRYPOINT ["citool"]
CMD ["--help"]
