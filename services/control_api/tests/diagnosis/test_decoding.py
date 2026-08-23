import pytest

from app.modules.diagnosis.decoding import DecodeError, decode_register_words


def test_decode_uses_published_word_order_instead_of_guessing_a_plausible_value():
    decoded = decode_register_words(
        [0x0000, 0x4120],
        data_type="float32",
        word_order="little",
        byte_order="big",
        scale=1,
        offset=0,
        expected_rule={"minimum": 0, "maximum": 20},
    )

    assert decoded["raw_value"] == pytest.approx(10.0)
    assert decoded["engineering_value"] == pytest.approx(10.0)
    assert decoded["within_expected_range"] is True
    assert decoded["applied_config"] == {
        "data_type": "float32",
        "word_order": "little",
        "byte_order": "big",
        "scale": 1.0,
        "offset": 0.0,
    }


def test_decode_reports_implausibility_without_trying_another_endianness():
    decoded = decode_register_words(
        [0x0000, 0x4120],
        data_type="float32",
        word_order="big",
        byte_order="big",
        scale=1,
        offset=0,
        expected_rule={"minimum": 1, "maximum": 20},
    )

    assert decoded["raw_value"] != pytest.approx(10.0)
    assert decoded["within_expected_range"] is False


def test_decode_rejects_a_word_count_that_does_not_match_the_profile_type():
    with pytest.raises(DecodeError, match="requires 2 register words"):
        decode_register_words(
            [0x4120],
            data_type="float32",
            word_order="big",
            byte_order="big",
        )


def test_decode_normalizes_the_shared_float_alias():
    decoded = decode_register_words(
        [0x4120, 0x0000],
        data_type="float",
        word_order="big",
        byte_order="big",
    )

    assert decoded["raw_value"] == pytest.approx(10.0)
    assert decoded["applied_config"]["data_type"] == "float32"
