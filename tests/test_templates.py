"""Tests for service template rendering."""

import pytest

from satdeploy.config import ModuleConfig
from satdeploy.templates import render_service_template


class TestRenderServiceTemplate:
    """Test service template rendering."""

    def test_replaces_csp_addr_placeholder(self):
        """Should replace {{ csp_addr }} with module's csp_addr value."""
        module = ModuleConfig(
            name="som1",
            host="192.168.1.10",
            user="root",
            csp_addr=5421,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )
        template = "ExecStart=/usr/bin/app {{ csp_addr }}"

        result = render_service_template(template, module)

        assert result == "ExecStart=/usr/bin/app 5421"

    def test_replaces_all_placeholders(self):
        """Should replace all supported placeholders."""
        module = ModuleConfig(
            name="som1",
            host="192.168.1.10",
            user="root",
            csp_addr=5421,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )
        template = (
            "ExecStart=/usr/bin/app "
            "{{ csp_addr }} {{ netmask }} {{ interface }} {{ baudrate }} "
            "-v {{ vmem_path }}"
        )

        result = render_service_template(template, module)

        assert result == (
            "ExecStart=/usr/bin/app "
            "5421 8 0 100000 "
            "-v /home/root/a53vmem"
        )
