"""Fleet-level operations across modules."""

from satdeploy.config import Config
from satdeploy.deployer import Deployer
from satdeploy.history import History


class FleetManager:
    """Manages fleet-level operations across multiple modules."""

    def __init__(self, config: Config, history: History, deployer: Deployer):
        self.config = config
        self.history = history
        self.deployer = deployer

    def get_status(self) -> dict:
        """Get status of all modules and apps.

        Returns:
            Dict keyed by module name containing status info.
        """
        modules = self.config.get_modules()
        app_names = self.config.get_all_app_names()
        result = {}

        for name, module in modules.items():
            online = self.deployer.check_module_online(module)
            apps = {}

            if online:
                # Get live hashes from remote
                for app_name in app_names:
                    app = self.config.get_app(app_name)
                    remote_hash = self.deployer.get_remote_hash(module, app.remote)
                    if remote_hash:
                        apps[app_name] = {"hash": remote_hash}
            else:
                # Use last known state from history
                state = self.history.get_module_state(name)
                for app_name, record in state.items():
                    apps[app_name] = {
                        "hash": record.binary_hash,
                        "last_deployed": record.timestamp,
                    }

            result[name] = {"online": online, "apps": apps}
        return result

    def diff_modules(self, module1: str, module2: str) -> dict:
        """Compare two modules and return differences.

        Args:
            module1: First module name.
            module2: Second module name.

        Returns:
            Dict mapping app_name to {module1: hash, module2: hash, match: bool}.
        """
        state1 = self.history.get_module_state(module1)
        state2 = self.history.get_module_state(module2)

        all_apps = set(state1.keys()) | set(state2.keys())
        result = {}

        for app_name in all_apps:
            hash1 = state1[app_name].binary_hash if app_name in state1 else None
            hash2 = state2[app_name].binary_hash if app_name in state2 else None
            result[app_name] = {
                module1: hash1,
                module2: hash2,
                "match": hash1 == hash2,
            }

        return result
