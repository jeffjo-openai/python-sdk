import json
import os
import unittest
import unittest.mock
import time
from statsig import __version__
from unittest.mock import patch
from tests.network_stub import NetworkStub
from statsig import statsig, StatsigUser, StatsigOptions, StatsigEnvironmentTier
from statsig.utils import logger

with open(os.path.join(os.path.abspath(os.path.dirname(__file__)), '../testdata/download_config_specs.json')) as r:
    CONFIG_SPECS_RESPONSE = r.read()

_network_stub = NetworkStub("http://test-retries")

@patch('requests.post', side_effect=_network_stub.mock)
class TestLoggingRetries(unittest.TestCase):
    
    @classmethod
    @patch('requests.post', side_effect=_network_stub.mock)
    def setUpClass(cls, mock_post):
        _network_stub.stub_request_with_value("download_config_specs", 200, json.loads(CONFIG_SPECS_RESPONSE))

        def on_log(url: str, data: dict):
            raise ConnectionError

        _network_stub.stub_request_with_function("log_event", 202, on_log)
                
        cls.statsig_user = StatsigUser(
            "regular_user_id", email="testuser@statsig.com", private_attributes={"test": 123})
        cls.random_user = StatsigUser("random")
        cls._logs = {}
        options = StatsigOptions(
            api=_network_stub.host,
            tier=StatsigEnvironmentTier.development,
            logging_interval=1)

        statsig.initialize("secret-test", options)
        cls.initTime = round(time.time() * 1000)
        logger.disabled = False

    def test_a_check_gate(self, mock_post):
        self.assertEqual(
            statsig.check_gate(self.statsig_user, "always_on_gate"),
            True
        )
        statsig.get_instance()._logger._flush(); # type: ignore - its set at this point
        time.sleep(12)