# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from typing import Dict, TextIO, Any

from xsdata.formats.dataclass.serializers.config import SerializerConfig
from xsdata.formats.dataclass.serializers.writers import XmlEventWriter


class XmlWriter(XmlEventWriter):
    """
    Custom xsdata XmlWritter with configurable indentation.

    TODO: Would be nice if this indentation change was added to upstream instead of hacking it here.
    """

    def __init__(self, config: SerializerConfig, output: TextIO, ns_map: Dict[Any, Any],
                 base_indentation_length: int = 1):
        super().__init__(config, output, ns_map)
        self.base_indentation_length = base_indentation_length

    def start_tag(self, qname: str) -> None:
        super(XmlEventWriter, self).start_tag(qname)

        if self.config.pretty_print:
            if self.current_level:
                # Suppressing 'Call to untyped function "ignorableWhitespace" in typed context'
                self.handler.ignorableWhitespace("\n")  # type: ignore
                self.handler.ignorableWhitespace(  # type: ignore
                    " " * self.current_level * self.base_indentation_length
                )

            self.current_level += 1
            self.pending_end_element = False

    def end_tag(self, qname: str) -> None:
        if not self.config.pretty_print:
            super(XmlEventWriter, self).end_tag(qname)
            return

        self.current_level -= 1
        if self.pending_end_element:
            self.handler.ignorableWhitespace("\n")  # type: ignore
            self.handler.ignorableWhitespace(  # type: ignore
                " " * self.current_level * self.base_indentation_length
            )

        super(XmlEventWriter, self).end_tag(qname)

        self.pending_end_element = True
        if not self.current_level:
            self.handler.ignorableWhitespace("\n")  # type: ignore
