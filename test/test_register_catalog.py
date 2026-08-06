from pathlib import Path

import pytest

from camera_debug_studio.register_catalog import RegisterCatalog, RegisterCatalogError


CATALOG = RegisterCatalog(Path(__file__).resolve().parents[1] / "registers" / "devices")


def test_ctrl3_value_da_reports_normal_status():
    result = CATALOG.decode("MAX96712", "0x1A", "0xDA")
    assert result["status"] == {"level": "normal", "summary": "状态正常"}
    assert {field["id"]: (field["value"], field["status"]) for field in result["fields"]} == {
        "LOCKED": (1, "normal"), "ERROR": (0, "normal"), "CMU_LOCKED": (1, "normal")}


def test_ctrl3_asserted_error_reports_error():
    result = CATALOG.decode("MAX96712", "CTRL3", "0x0E")
    assert result["status"]["level"] == "error"
    assert next(field for field in result["fields"] if field["id"] == "ERROR")["meaning"] == "ERRB 已触发（ERRB pin = 0）"


def test_address_index_without_detail_is_explicitly_rejected():
    with pytest.raises(RegisterCatalogError, match="尚未录入位域详情"):
        CATALOG.decode("MAX96712", "0x00", "0x52")
