import unittest

from growatt_power_guard import (
    Config,
    DeviceRef,
    extract_soc,
    extract_spf_output_source,
    render_params,
    set_mode,
    build_parser,
)


class GrowattPowerGuardTests(unittest.TestCase):
    def test_render_params_replaces_placeholders_inside_json(self):
        device = DeviceRef("plant123", "SN123", "storage", {})
        template = (
            '{"op":"storageSet","serialNum":"{device_sn}",'
            '"plant":"{plant_id}","mode":"{mode}","param1":"2"}'
        )

        self.assertEqual(
            render_params(template, device, "sbu"),
            {
                "op": "storageSet",
                "serialNum": "SN123",
                "plant": "plant123",
                "mode": "sbu",
                "param1": "2",
            },
        )

    def test_extract_soc_finds_nested_percentage(self):
        status = {"storage_params": {"storageDetailBean": {"capacity": "44%"}}}

        self.assertEqual(extract_soc(status), (44.0, "storage_params.storageDetailBean.capacity"))

    def test_extract_spf_output_source(self):
        status = {"storage_params": {"storageDetailBean": {"outputConfig": 2}}}

        self.assertEqual(
            extract_spf_output_source(status),
            ("2", "Utility first", "storage_params.storageDetailBean.outputConfig"),
        )

    def test_spf5000_driver_prepares_expected_dry_run_params(self):
        config = Config(
            username="u",
            password="p",
            server_url="https://openapi.growatt.com/",
            plant_id="plant123",
            device_sn="SN123",
            low_battery_soc=45,
            dry_run=True,
            mode_driver="spf5000",
            set_mode_path="tcpSet.do",
            set_mode_method="post",
            utility_mode_params="",
            sbu_mode_params="",
        )
        device = DeviceRef("plant123", "SN123", "storage", {})

        self.assertEqual(
            set_mode(None, config, device, "utility"),
            {
                "dry_run": True,
                "mode": "utility",
                "path": "tcpSet.do",
                "method": "post_params",
                "params": {
                    "action": "storageSPF5000Set",
                    "serialNum": "SN123",
                    "type": "storage_spf5000_ac_output_source",
                    "param1": "2",
                    "param2": "",
                    "param3": "",
                    "param4": "",
                },
            },
        )

    def test_preserve_battery_command_is_available(self):
        args = build_parser().parse_args(["preserve-battery"])

        self.assertEqual(args.command, "preserve-battery")


if __name__ == "__main__":
    unittest.main()
