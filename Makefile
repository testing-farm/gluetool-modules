.DEFAULT_GOAL := help

.PHONY := build push clean edit-image-test test-image help

# default image tag set to current user name
IMAGE := quay.io/testing-farm/gluetool-modules
IMAGE_TAG ?= ${USER}

##@ Image

build:  ## Build gluetool-modules container image
	poetry build
	buildah bud --layers -t $(IMAGE):$(IMAGE_TAG) -f container/Dockerfile .

push:  ## Push gluetool-modules container image to quay.io
	buildah push $(IMAGE):$(IMAGE_TAG)

##@ Test

test-image:  ## Test container image via dgoss
	cd container && dgoss run -t --entrypoint bash $(IMAGE):$(IMAGE_TAG)

##@ Utility

clean:  ## Remove gluetool-modules container image
	buildah rmi $(IMAGE):$(IMAGE_TAG)

edit-image-test:  ## Edit goss file via dgoss
	cd container && dgoss edit -t --entrypoint bash $(IMAGE):$(IMAGE_TAG)

install-cs9:  ## Install required system dependencies in CentOS Stream 9
	sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
		ansible autoconf automake gcc git krb5-devel libcurl-devel libtool \
		libxml2-devel openssl-devel popt-devel postgresql-devel python3-devel

install-fedora:  ## Install required system dependencies in Fedora
	sudo dnf -y install ansible autoconf automake gcc git krb5-devel libcurl-devel \
		libpq-devel libtool libxml2-devel openssl-devel popt-devel postgresql-devel \
		python3-libselinux redhat-rpm-config python3-rpm python3.9

# See https://www.thapaliya.com/en/writings/well-documented-makefiles/ for details.
help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make [target]\033[36m\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)
