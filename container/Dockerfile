# Citool container with gluetool-modules-framework modules
#
# Volume mounts:
#    /var/ARTIFACTS     - volume mount for artifacts storage
#    /CONFIG            - place for configuration
#

# NOTE: last version on Fedora 42
FROM quay.io/containers/podman:v5.6.1
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

# tmt policies
ENV TMT_POLICY_ROOT=/etc/tmt/policies

# python version
ENV PYTHON_VERSION=3.12

# Ansible requirements
COPY container/requirements.yml /etc/ansible/requirements.yml

# ~/.gitconfig is required for some operations of the git library in gluetool-modules
COPY container/.gitconfig /root/.gitconfig

# make sure we have latest packages installed
RUN dnf -y update

# install all required dependencies
RUN    ln -s /CONFIG /etc/citool.d \
    && dnf -y install ansible-core autoconf automake crun gcc git krb5-devel libcurl-devel libffi-devel \
                      libpq-devel libtool libxml2-devel make openssl-devel popt-devel \
                      postgresql-devel python${PYTHON_VERSION}-devel python3-libselinux \
                      redhat-rpm-config koji python3-virtualenv \
                      rsync beakerlib-libraries rpm-build rpm-devel awscli \
    # Pull in tmt from @teemtee/stable copr repository
    && dnf copr -y enable @teemtee/stable \
    && dnf -y install fmf-1.7.0 \
                      tmt-1.65.0 \
                      tmt+provision-container-1.65.0 \
                      tmt+provision-virtual-1.65.0 \
                      tmt+report-polarion-1.65.0 \
    # Pull in tmt policies
    && mkdir -p /etc/tmt/policies \
    && git clone --branch main --depth 1 https://gitlab.com/testing-farm/profiles.git /tmp/profiles \
    && cp -rv /tmp/profiles/policies/* ${TMT_POLICY_ROOT}/ \
    && find ${TMT_POLICY_ROOT} -type f -print0 | xargs -0 chmod "ugo=r" \
    && rm -rf /tmp/profiles \
    # common SSH configuration for testing
    && bash -c "echo 'Host *' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  StrictHostKeyChecking no' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  UserKnownHostsFile /dev/null' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  SendEnv -LC_* -LANG_*' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  ServerAliveInterval 60' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  ServerAliveCountMax 5' >> /etc/ssh/ssh_config" \
    # TFT-3381 - needed to support RHEL6
    && bash -c "echo '  HostKeyAlgorithms +ssh-rsa' >> /etc/ssh/ssh_config" \
    && bash -c "echo '  PubkeyAcceptedKeyTypes +ssh-rsa' >> /etc/ssh/ssh_config" \
    && bash -c "sed -i '/\[ evp_properties \]/a rh-allow-sha1-signatures = yes' /etc/ssl/openssl.cnf" \
    && bash -c "sed -i 's/^rh-allow-sha1-signatures = no$/rh-allow-sha1-signatures = yes/g' /etc/crypto-policies/back-ends/opensslcnf.config" \
    # Install standard-test-roles from git
    # NOTE(mvadkert): there is no better way, mimics https://src.fedoraproject.org/rpms/standard-test-roles/blob/f42/f/standard-test-roles.spec
    && git clone --depth 1 https://pagure.io/standard-test-roles.git /tmp/standard-test-roles \
    && cp -rv /tmp/standard-test-roles/roles/standard-test-* /tmp/standard-test-roles/roles/str-common-* /etc/ansible/roles/ \
    && cp -f /tmp/standard-test-roles/scripts/str-filter-tests /usr/bin \
    && chmod +x /usr/bin/str-filter-tests \
    && rm -rf /tmp/standard-test-roles

# copy the built package
COPY dist/gluetool_modules_framework*.whl .

# install gluetool-modules-framework with citool and required ansible collections
RUN    virtualenv -p python${PYTHON_VERSION} /opt/gluetool \
    && source /opt/gluetool/bin/activate \
    && pip${PYTHON_VERSION} install gluetool_modules_framework*.whl \
    && ansible-galaxy install --timeout 120 -r /etc/ansible/requirements.yml \
    && rm -f gluetool_modules_framework*.whl \
    && rm -rf ~/.cache

# install artemis
RUN    virtualenv -p python${PYTHON_VERSION} /opt/artemis-cli \
    && source /opt/artemis-cli/bin/activate \
    && pip${PYTHON_VERSION} install tft-artemis-cli \
    && rm -rf ~/.cache

# run citool as entrypoint
ENTRYPOINT ["citool"]
CMD ["--help"]
