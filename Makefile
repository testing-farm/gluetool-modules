.DEFAULT_GOAL := help

# default image tag set to current user name
IMAGE_TAG ?= ${USER}

build:  ## Build gluetool-modules container image
	poetry build
	buildah bud --layers -t quay.io/testing-farm/gluetool-modules:$(IMAGE_TAG) -f container/Dockerfile .

push:  ## Push gluetool-modules container image to quay.io
	buildah push quay.io/testing-farm/gluetool-modules:$(IMAGE_TAG)

clean:  ## Remove gluetool-modules container image
	buildah rmi quay.io/testing-farm/cli:$(IMAGE_TAG)

edit-image-test:  ## Edit goss file via dgoss
	cd container && dgoss edit -t --entrypoint bash quay.io/testing-farm/gluetool-modules:$(IMAGE_TAG)

test-image:  ## Test container image via dgoss
	cd container && dgoss run -t --entrypoint bash quay.io/testing-farm/gluetool-modules:$(IMAGE_TAG)

# See https://www.thapaliya.com/en/writings/well-documented-makefiles/ for details.
help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make [target]\033[36m\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)
