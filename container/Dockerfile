#
# Container to run citool with gluetool modules
#
# Volume mounts:
#
#    /var/ARTIFACTS - all artifacts will be saved here
#    /CONFIG - place for configuration
#

# Based on CentOS 8 podman image
# https://github.com/thrix/podman/blob/main/contrib/podmanimage/stable/Dockerfile.centos8
FROM quay.io/testing-farm/podman:latest
LABEL maintainer="tft@redhat.com"

# workdir, needs to be writable in all supported composes, even those based on rpm-ostree
WORKDIR /var/ARTIFACTS

# pycurl needs to be compiled against openssl to work properly
ENV PYCURL_SSL_BACKEND=openssl

# install all required dependencies
# WORKAROUND: use latest tmt from a copr to have TMT_TREE available
RUN    ln -s /CONFIG /etc/citool.d \
    && rpm -Uvh https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
    && dnf -y install ansible autoconf automake gcc git krb5-devel libcurl-devel \
                      libpq-devel libtool libxml2-devel make openssl-devel popt-devel \
                      postgresql-devel python3-libselinux python2-devel \
                      redhat-rpm-config standard-test-roles koji python3-virtualenv \
                      rsync python3 beakerlib-libraries \
                      tmt-provision-container tmt-provision-virtual \
    && dnf -y update --enablerepo=epel-testing tmt-provision-container tmt-provision-virtual \
    && curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python3 - \
    && dnf clean all \
    && bash -c "echo 'Host *' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  StrictHostKeyChecking no' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  UserKnownHostsFile /dev/null' >> /etc/ssh/ssh_config"

# copy the built package
COPY . /opt/gluetool-modules

# install gluetool-modules & citool
RUN    cd /opt/gluetool-modules \
    && /root/.poetry/bin/poetry config virtualenvs.create false \
    && virtualenv -p python2 /opt/gluetool-venv \
    && source /opt/gluetool-venv/bin/activate \
    && /root/.poetry/bin/poetry install \
    && ln -s /opt/gluetool-venv/bin/citool /usr/bin/citool \
    && ln -s /opt/gluetool-venv/bin/gluetool /usr/bin/gluetool \
    && rm -rf ~/.cache

# run citool as entrypoint
ENTRYPOINT ["/usr/bin/citool"]
CMD ["--help"]
