"""Tests for the fleet module."""

from unittest.mock import Mock

import pytest

from satdeploy.config import ModuleConfig
from satdeploy.fleet import FleetManager
from satdeploy.history import DeploymentRecord


class TestFleetManagerInit:
    """Tests for FleetManager initialization."""

    def test_fleet_manager_accepts_dependencies(self):
        """FleetManager should accept config, history, and deployer."""
        config = Mock()
        history = Mock()
        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)

        assert fleet.config is config
        assert fleet.history is history
        assert fleet.deployer is deployer


class TestGetStatus:
    """Tests for FleetManager.get_status()."""

    def test_get_status_returns_dict_with_modules(self):
        """get_status should return dict keyed by module name."""
        config = Mock()
        config.get_modules.return_value = {
            "som1": ModuleConfig(
                name="som1",
                host="192.168.1.10",
                user="root",
                csp_addr=5421,
                netmask=8,
                interface=0,
                baudrate=100000,
                vmem_path="/home/root/a53vmem",
            ),
        }
        config.get_all_app_names.return_value = ["controller"]

        history = Mock()
        history.get_module_state.return_value = {}

        deployer = Mock()
        deployer.check_module_online.return_value = False

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        status = fleet.get_status()

        assert isinstance(status, dict)
        assert "som1" in status

    def test_get_status_includes_online_offline(self):
        """get_status should include online/offline flag for each module."""
        config = Mock()
        config.get_modules.return_value = {
            "som1": ModuleConfig(
                name="som1",
                host="192.168.1.10",
                user="root",
                csp_addr=5421,
                netmask=8,
                interface=0,
                baudrate=100000,
                vmem_path="/home/root/a53vmem",
            ),
        }
        config.get_all_app_names.return_value = []

        history = Mock()
        history.get_module_state.return_value = {}

        deployer = Mock()
        deployer.check_module_online.return_value = True

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        status = fleet.get_status()

        assert "online" in status["som1"]
        assert status["som1"]["online"] is True

    def test_get_status_shows_offline_module(self):
        """get_status should show offline when module is unreachable."""
        config = Mock()
        config.get_modules.return_value = {
            "som1": ModuleConfig(
                name="som1",
                host="192.168.1.10",
                user="root",
                csp_addr=5421,
                netmask=8,
                interface=0,
                baudrate=100000,
                vmem_path="/home/root/a53vmem",
            ),
        }
        config.get_all_app_names.return_value = []

        history = Mock()
        history.get_module_state.return_value = {}

        deployer = Mock()
        deployer.check_module_online.return_value = False

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        status = fleet.get_status()

        assert status["som1"]["online"] is False

    def test_get_status_includes_apps_dict(self):
        """get_status should include apps dict for each module."""
        config = Mock()
        config.get_modules.return_value = {
            "som1": ModuleConfig(
                name="som1",
                host="192.168.1.10",
                user="root",
                csp_addr=5421,
                netmask=8,
                interface=0,
                baudrate=100000,
                vmem_path="/home/root/a53vmem",
            ),
        }
        config.get_all_app_names.return_value = ["controller"]

        history = Mock()
        history.get_module_state.return_value = {}

        deployer = Mock()
        deployer.check_module_online.return_value = False

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        status = fleet.get_status()

        assert "apps" in status["som1"]
        assert isinstance(status["som1"]["apps"], dict)

    def test_get_status_offline_uses_history(self):
        """When module is offline, get_status should use last known state from history."""
        config = Mock()
        config.get_modules.return_value = {
            "som1": ModuleConfig(
                name="som1",
                host="192.168.1.10",
                user="root",
                csp_addr=5421,
                netmask=8,
                interface=0,
                baudrate=100000,
                vmem_path="/home/root/a53vmem",
            ),
        }
        config.get_all_app_names.return_value = ["controller"]

        history = Mock()
        history.get_module_state.return_value = {
            "controller": DeploymentRecord(
                module="som1",
                app="controller",
                binary_hash="abc12345",
                remote_path="/usr/bin/controller",
                action="push",
                success=True,
                timestamp="2024-01-15T10:00:00",
            )
        }

        deployer = Mock()
        deployer.check_module_online.return_value = False

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        status = fleet.get_status()

        assert "controller" in status["som1"]["apps"]
        assert status["som1"]["apps"]["controller"]["hash"] == "abc12345"
        assert status["som1"]["apps"]["controller"]["last_deployed"] == "2024-01-15T10:00:00"

    def test_get_status_online_uses_live_hash(self):
        """When module is online, get_status should get live hash from remote."""
        from satdeploy.config import AppConfig

        config = Mock()
        config.get_modules.return_value = {
            "som1": ModuleConfig(
                name="som1",
                host="192.168.1.10",
                user="root",
                csp_addr=5421,
                netmask=8,
                interface=0,
                baudrate=100000,
                vmem_path="/home/root/a53vmem",
            ),
        }
        config.get_all_app_names.return_value = ["controller"]
        config.get_app.return_value = AppConfig(
            name="controller",
            local="./build/controller",
            remote="/usr/bin/controller",
            service="controller.service",
            service_template=None,
            vmem_dir=None,
        )

        history = Mock()
        history.get_module_state.return_value = {}

        deployer = Mock()
        deployer.check_module_online.return_value = True
        deployer.get_remote_hash.return_value = "live1234"

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        status = fleet.get_status()

        assert "controller" in status["som1"]["apps"]
        assert status["som1"]["apps"]["controller"]["hash"] == "live1234"


class TestDiffModules:
    """Tests for FleetManager.diff_modules()."""

    def test_diff_modules_returns_dict_keyed_by_app(self):
        """diff_modules should return dict keyed by app name."""
        config = Mock()
        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="abc12345",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="abc12345",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        diff = fleet.diff_modules("som1", "som2")

        assert isinstance(diff, dict)
        assert "controller" in diff

    def test_diff_modules_includes_hashes_for_both_modules(self):
        """diff_modules should include hash for each module."""
        config = Mock()
        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="hash_som1",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="hash_som2",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        diff = fleet.diff_modules("som1", "som2")

        assert diff["controller"]["som1"] == "hash_som1"
        assert diff["controller"]["som2"] == "hash_som2"

    def test_diff_modules_match_true_when_hashes_equal(self):
        """diff_modules match should be True when hashes are equal."""
        config = Mock()
        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="same_hash",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="same_hash",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        diff = fleet.diff_modules("som1", "som2")

        assert diff["controller"]["match"] is True

    def test_diff_modules_match_false_when_hashes_differ(self):
        """diff_modules match should be False when hashes differ."""
        config = Mock()
        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="hash_v1",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="hash_v2",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        diff = fleet.diff_modules("som1", "som2")

        assert diff["controller"]["match"] is False

    def test_diff_modules_handles_app_on_one_module_only(self):
        """diff_modules should handle app deployed to only one module."""
        config = Mock()
        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {
                "controller": DeploymentRecord(
                    module="som1", app="controller", binary_hash="ctrl_hash",
                    remote_path="/usr/bin/controller", action="push", success=True,
                ),
                "unique_app": DeploymentRecord(
                    module="som1", app="unique_app", binary_hash="unique_hash",
                    remote_path="/usr/bin/unique_app", action="push", success=True,
                ),
            },
            "som2": {
                "controller": DeploymentRecord(
                    module="som2", app="controller", binary_hash="ctrl_hash",
                    remote_path="/usr/bin/controller", action="push", success=True,
                ),
            },
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        diff = fleet.diff_modules("som1", "som2")

        assert "unique_app" in diff
        assert diff["unique_app"]["som1"] == "unique_hash"
        assert diff["unique_app"]["som2"] is None
        assert diff["unique_app"]["match"] is False


class TestSyncModules:
    """Tests for FleetManager.sync_modules()."""

    def test_sync_modules_deploys_differing_apps_to_target(self):
        """sync_modules should deploy apps that differ to the target module."""
        from satdeploy.config import AppConfig

        config = Mock()
        config.get_module.return_value = ModuleConfig(
            name="som2",
            host="192.168.1.11",
            user="root",
            csp_addr=5475,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )
        config.get_app.return_value = AppConfig(
            name="controller",
            local="./build/controller",
            remote="/usr/bin/controller",
            service="controller.service",
            service_template=None,
            vmem_dir=None,
        )

        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="hash_v2",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="hash_v1",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()
        deployer.compute_hash.return_value = "hash_v2"

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        fleet.sync_modules("som1", "som2")

        # Should call deploy for the differing app
        deployer.deploy.assert_called()

    def test_sync_modules_skips_matching_apps(self):
        """sync_modules should not deploy apps that already match."""
        from satdeploy.config import AppConfig

        config = Mock()
        config.get_module.return_value = ModuleConfig(
            name="som2",
            host="192.168.1.11",
            user="root",
            csp_addr=5475,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )
        config.get_app.return_value = AppConfig(
            name="controller",
            local="./build/controller",
            remote="/usr/bin/controller",
            service="controller.service",
            service_template=None,
            vmem_dir=None,
        )

        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="same_hash",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="same_hash",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        fleet.sync_modules("som1", "som2")

        # Should NOT call deploy since hashes match
        deployer.deploy.assert_not_called()

    def test_sync_modules_clears_vmem_when_requested(self):
        """sync_modules should clear vmem when clean_vmem is True."""
        from satdeploy.config import AppConfig

        config = Mock()
        config.get_module.return_value = ModuleConfig(
            name="som2",
            host="192.168.1.11",
            user="root",
            csp_addr=5475,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )
        config.get_app.return_value = AppConfig(
            name="controller",
            local="./build/controller",
            remote="/usr/bin/controller",
            service="controller.service",
            service_template=None,
            vmem_dir="/home/root/ctrlvmem",
        )

        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="hash_v2",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="hash_v1",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        fleet.sync_modules("som1", "som2", clean_vmem=True)

        # Should call clear_vmem_dir for the app's vmem_dir
        deployer.clear_vmem_dir.assert_called_with("/home/root/ctrlvmem")

    def test_sync_modules_uploads_service_template(self):
        """sync_modules should render and upload service template for target."""
        from satdeploy.config import AppConfig

        target_module = ModuleConfig(
            name="som2",
            host="192.168.1.11",
            user="root",
            csp_addr=5475,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )

        config = Mock()
        config.get_module.return_value = target_module
        config.get_app.return_value = AppConfig(
            name="controller",
            local="./build/controller",
            remote="/usr/bin/controller",
            service="controller.service",
            service_template="ExecStart=/usr/bin/controller {{ csp_addr }}",
            vmem_dir=None,
        )

        history = Mock()
        history.get_module_state.side_effect = lambda m: {
            "som1": {"controller": DeploymentRecord(
                module="som1", app="controller", binary_hash="hash_v2",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
            "som2": {"controller": DeploymentRecord(
                module="som2", app="controller", binary_hash="hash_v1",
                remote_path="/usr/bin/controller", action="push", success=True,
            )},
        }[m]

        deployer = Mock()

        fleet = FleetManager(config=config, history=history, deployer=deployer)
        fleet.sync_modules("som1", "som2")

        # Should call upload_service with rendered template containing target's csp_addr
        deployer.upload_service.assert_called()
        call_args = deployer.upload_service.call_args
        assert "controller.service" in call_args[0]
        assert "5475" in call_args[0][1]  # Target's csp_addr should be in content
