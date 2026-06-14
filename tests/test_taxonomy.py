from app.enums import ConsoleModel, PartType
from app.taxonomy import classify_model, classify_part, detect_shipping


def test_oled_takes_priority():
    assert classify_model("Nintendo Switch OLED blanche") is ConsoleModel.OLED


def test_lite_detected():
    assert classify_model("switch lite corail HS") is ConsoleModel.LITE


def test_v2_detected():
    assert classify_model("Console Switch V2 autonomie améliorée") is ConsoleModel.V2


def test_v1_detected():
    assert classify_model("Switch V1 première génération") is ConsoleModel.V1


def test_unknown_model():
    assert classify_model("jeu Mario Kart 8") is ConsoleModel.UNKNOWN


def test_job_lot_part():
    assert classify_part("Lot de 3 Nintendo Switch") is PartType.JOB_LOT


def test_for_parts_french():
    assert classify_part("Switch pour pièces ne s'allume plus") is PartType.FOR_PARTS


def test_for_parts_hs_keyword():
    assert classify_part("Nintendo Switch HS") is PartType.FOR_PARTS


def test_motherboard():
    assert classify_part("Carte mère Switch OLED") is PartType.MOTHERBOARD


def test_screen():
    assert classify_part("Écran LCD pour Switch") is PartType.SCREEN


def test_chassis():
    assert classify_part("Coque arrière châssis Switch Lite") is PartType.CHASSIS


def test_for_parts_beats_component():
    # "for parts" is a stronger buy signal than a generic component mention.
    assert classify_part("Switch en panne avec carte mere HS") is PartType.FOR_PARTS


def test_shipping_detection():
    found = detect_shipping("Envoi Mondial Relay ou Vinted Go possible")
    assert "mondial_relay" in found
    assert "vinted_go" in found


def test_shipping_hand_delivery():
    assert "hand_delivery" in detect_shipping("Remise en main propre uniquement")
