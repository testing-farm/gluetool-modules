.DEFAULT_GOAL := help

.PHONY := build push clean edit-image-test test-image help

# default image tag set to current user name
IMAGE := quay.io/testing-farm/gluetool-modules
IMAGE_TAG ?= ${USER}

##@ Image

build:  ## Build gluetool-modules container image
	poetry build
	buildah bud --pull=always --layers -t $(IMAGE):$(IMAGE_TAG) -f container/Dockerfile .

push:  ## Push gluetool-modules container image to quay.io
	buildah push $(IMAGE):$(IMAGE_TAG)

##@ Test

test-image:  ## Test container image via dgoss
	cd container && dgoss run --stop-timeout 0 -t --entrypoint bash $(IMAGE):$(IMAGE_TAG)

test-coala:  ## Run coala static analysis
	 podman run -ti --rm -v $$PWD:/gluetool_modules_framework:z --workdir=/gluetool_modules_framework \
		 docker.io/coala/base coala -c /gluetool_modules_framework/.coafile --non-interactive

##@ Utility

clean:  ## Remove gluetool-modules container image
	buildah rmi $(IMAGE):$(IMAGE_TAG)

edit-image-test:  ## Edit goss file via dgoss
	cd container && dgoss edit --stop-timeout 0 --entrypoint bash $(IMAGE):$(IMAGE_TAG)

install-cs10:  ## Install required system dependencies in CentOS Stream 10
	sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm
	sudo dnf -y install ansible-core autoconf automake gcc git krb5-devel libcurl-devel libtool libffi-devel \
		libxml2-devel openssl-devel popt-devel postgresql-devel python3-devel

install-fedora:  ## Install required system dependencies in Fedora
	sudo dnf -y install ansible autoconf automake gcc git krb5-devel libcurl-devel libffi-devel \
		libpq-devel libtool libxml2-devel libxslt-devel openssl-devel popt-devel postgresql-devel \
		python3-libselinux redhat-rpm-config python3-rpm python3.12

# See https://www.thapaliya.com/en/writings/well-documented-makefiles/ for details.
help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make [target]\033[36m\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)
