import json
import logging
import threading

from .statsig_error_boundary import _StatsigErrorBoundary
from .statsig_errors import StatsigValueError, StatsigNameError
from .statsig_network import _StatsigNetwork
from .statsig_options import StatsigOptions

RULESETS_SYNC_INTERVAL = 10
IDLISTS_SYNC_INTERVAL = 60
STORAGE_ADAPTER_KEY = "statsig.cache"


def _is_specs_json_valid(specs_json):
    if specs_json is None or specs_json.get("time") is None:
        return False
    if specs_json.get("has_updates", False) is False:
        return False

    return True


class _SpecStore:
    def __init__(self, network: _StatsigNetwork, options: StatsigOptions, statsig_metadata: dict,
                 error_boundary: _StatsigErrorBoundary, shutdown_event: threading.Event):
        self._network = network
        self._options = options
        self._statsig_metadata = statsig_metadata
        self._error_boundary = error_boundary
        self._shutdown_event = shutdown_event

        self._configs = dict()
        self._gates = dict()
        self._layers = dict()
        self._experiment_to_layer = dict()
        self._last_update_time = 0

        self._id_lists = dict()

        if not options.local_mode:
            self._initialize_specs()
            self._download_id_lists()

            self._background_download_configs = self._spawn_background_sync_thread(
                (self._download_config_specs, options.rulesets_sync_interval or RULESETS_SYNC_INTERVAL))
            self._background_download_idlists = self._spawn_background_sync_thread(
                (self._download_id_lists, options.idlists_sync_interval or IDLISTS_SYNC_INTERVAL))

    def is_ready_for_checks(self):
        return self._last_update_time != 0

    def shutdown(self):
        if self._options.local_mode:
            return

        self._background_download_configs.join()
        self._background_download_idlists.join()

    def get_gate(self, name: str):
        return self._gates.get(name)

    def get_all_gates(self):
        return self._gates

    def get_config(self, name: str):
        return self._configs.get(name)

    def get_all_configs(self):
        return self._configs

    def get_layer(self, name: str):
        return self._layers.get(name)

    def get_all_layers(self):
        return self._layers

    def get_layer_name_for_experiment(self, experiment_name: str):
        return self._experiment_to_layer.get(experiment_name)

    def get_id_list(self, id_list_name):
        return self._id_lists.get(id_list_name)

    def get_all_id_lists(self):
        return self._id_lists

    def _initialize_specs(self):
        if self._options.data_store is not None:
            if self._options.bootstrap_values is not None:
                logging.getLogger('statsig.sdk').warning(
                    "data_store gets priority over bootstrap_values. bootstrap_values will be ignored")

            self._load_config_specs_from_storage_adapter()
            if self._last_update_time is 0:
                self._download_config_specs()

        elif self._options.bootstrap_values is not None:
            self._bootstrap_config_specs()

        else:
            self._download_config_specs()

    def _process_specs(self, specs_json):
        if not _is_specs_json_valid(specs_json):
            return

        def get_parsed_specs(key: str):
            parsed = dict()
            for spec in specs_json.get(key, []):
                spec_name = spec.get("name")
                if spec_name is not None:
                    parsed[spec_name] = spec
            return parsed

        new_gates = get_parsed_specs("feature_gates")
        new_configs = get_parsed_specs("dynamic_configs")
        new_layers = get_parsed_specs("layer_configs")

        new_experiment_to_layer = dict()
        layers_dict = specs_json.get("layers", {})
        for layer_name in layers_dict:
            experiments = layers_dict[layer_name]
            for experiment_name in experiments:
                new_experiment_to_layer[experiment_name] = layer_name

        self._gates = new_gates
        self._configs = new_configs
        self._layers = new_layers
        self._experiment_to_layer = new_experiment_to_layer
        self._last_update_time = specs_json.get("time", 0)

        if callable(self._options.rules_updated_callback):
            self._options.rules_updated_callback(json.dumps(specs_json))

    def _load_config_specs_from_storage_adapter(self):
        if self._options.data_store is None:
            return

        cache_string = self._options.data_store.get(STORAGE_ADAPTER_KEY)
        if not isinstance(cache_string, str):
            return

        cache = json.loads(cache_string)
        if not isinstance(cache, dict):
            logging.getLogger('statsig.sdk').warning("Invalid type returned from StatsigOptions.data_store")
            return

        adapter_time = cache.get("last_update_time", None)
        if not isinstance(adapter_time, int) or adapter_time < self._last_update_time:
            return

        self._gates = cache.get("gates", None) or self._gates
        self._configs = cache.get("configs", None) or self._configs
        self._layers = cache.get("layers", None) or self._layers
        self._experiment_to_layer = cache.get("experiment_to_layer", None) or self._experiment_to_layer
        self._last_update_time = cache.get("last_update_time", None) or self._last_update_time

    def _bootstrap_config_specs(self):
        if self._options.bootstrap_values is None:
            return

        try:
            specs = json.loads(self._options.bootstrap_values)
            if specs is None:
                return

            self._process_specs(specs)
        except ValueError:
            # JSON decoding failed, just let background thread update rulesets
            logging.getLogger('statsig.sdk').exception(
                'Failed to parse bootstrap_values')
            return

    def _download_config_specs(self):
        specs = self._network.post_request("download_config_specs", {
            "statsigMetadata": self._statsig_metadata,
            "sinceTime": self._last_update_time,
        })

        if not _is_specs_json_valid(specs):
            return

        self._process_specs(specs)
        self._save_to_storage_adapter()

    def _save_to_storage_adapter(self):
        if self._options.data_store is None:
            return

        if self._last_update_time is 0:
            return

        cache = {
            "gates": self._gates,
            "configs": self._configs,
            "layers": self._layers,
            "experiment_to_layer": self._experiment_to_layer,
            "last_update_time": self._last_update_time
        }
        self._options.data_store.set(STORAGE_ADAPTER_KEY, json.dumps(cache))

    def _download_id_lists(self):
        try:
            server_id_lists = self._network.post_request("get_id_lists", {
                "statsigMetadata": self._statsig_metadata,
            })
            if server_id_lists is None:
                return

            local_id_lists = self._id_lists
            thread_pool = []

            for list_name in server_id_lists:
                server_list = server_id_lists.get(list_name, dict())
                url = server_list.get("url", None)
                size = server_list.get("size", 0)
                local_list = local_id_lists.get(list_name, dict())

                new_creation_time = server_list.get("creationTime", 0)
                old_creation_time = local_list.get("creationTime", 0)
                new_file_id = server_list.get("fileID", None)
                old_file_id = local_list.get("fileID", "")

                if url is None or new_creation_time < old_creation_time or new_file_id is None:
                    continue

                # should reset the list if a new file has been created
                if new_file_id != old_file_id and new_creation_time >= old_creation_time:
                    local_list = {
                        "ids": set(),
                        "readBytes": 0,
                        "url": url,
                        "fileID": new_file_id,
                        "creationTime": new_creation_time,
                    }

                read_bytes = local_list.get("readBytes", 0)
                # check if read bytes count is the same as total file size;
                #  only download additional ids if sizes don't match
                if size <= read_bytes or url == "":
                    continue
                thread = threading.Thread(
                    target=self._download_single_id_list,
                    args=(url, list_name, local_list, local_id_lists, read_bytes,))
                thread.daemon = True
                thread_pool.append(thread)
                thread.start()

            for thread in thread_pool:
                thread.join()

            deleted_lists = []
            for list_name in local_id_lists:
                if list_name not in server_id_lists:
                    deleted_lists.append(list_name)

            # remove any list that has been deleted
            for list_name in deleted_lists:
                local_id_lists.pop(list_name, None)
        except Exception as e:
            self._error_boundary.log_exception(e)

    def _download_single_id_list(self, url, list_name, local_list, all_lists, start_index):
        resp = self._network.get_request(
            url, headers={"Range": "bytes=%s-" % start_index})
        if resp is None:
            return
        try:
            content_length_str = resp.headers.get('content-length')
            if content_length_str is None:
                raise StatsigValueError("Content length invalid.")
            content_length = int(content_length_str)
            content = resp.text
            if content is None:
                return
            first_char = content[0]
            if first_char != "+" and first_char != "-":
                raise StatsigNameError("Seek range invalid.")
            lines = content.splitlines()
            for line in lines:
                if len(line) <= 1:
                    continue
                op = line[0]
                id = line[1:].strip()
                if op == "+":
                    local_list.get("ids", set()).add(id)
                elif op == "-":
                    local_list.get("ids", set()).discard(id)
            local_list["readBytes"] = start_index + content_length
            all_lists[list_name] = local_list
        except Exception as e:
            self._error_boundary.log_exception(e)

    def _spawn_background_sync_thread(self, args=()):
        thread = threading.Thread(target=self._sync, args=args)
        thread.daemon = True
        thread.start()
        return thread

    def _sync(self, sync_func, interval):
        while True:
            try:
                if self._shutdown_event.wait(interval):
                    break
                sync_func()
            except Exception as e:
                self._error_boundary.log_exception(e)