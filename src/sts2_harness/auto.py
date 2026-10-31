"""Mechanical Run proposals use native controls and short original game-language reasons."""

from enum import StrEnum

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import ActionKind, ActionProposal, ActionStep


class _Reason(StrEnum):
    POTION = "potion"
    SHOP = "shop"
    MAP = "map"
    CHEST = "chest"
    CONTINUE = "continue"


# These are original operation explanations, not extracted game localization.
_SHOP_REASONS: dict[str, tuple[str, str]] = {
    "eng": (
        "Use the merchant potion before opening the shop.",
        "Open the shop to inspect its stock.",
    ),
    "zhs": ("开店前先使用商人药水。", "打开商店查看商品。"),
    "zht": ("開店前先使用商人藥水。", "打開商店查看商品。"),
    "deu": (
        "Vor dem Öffnen den Händlertrank verwenden.",
        "Den Laden öffnen, um das Angebot zu prüfen.",
    ),
    "esp": (
        "Usa la poción del mercader antes de abrir la tienda.",
        "Abre la tienda para revisar las mercancías.",
    ),
    "fra": (
        "Utiliser la potion du marchand avant d’ouvrir la boutique.",
        "Ouvrir la boutique pour examiner les articles.",
    ),
    "ind": ("Gunakan ramuan pedagang sebelum membuka toko.", "Buka toko untuk memeriksa barang."),
    "ita": (
        "Usa la pozione del mercante prima di aprire il negozio.",
        "Apri il negozio per esaminare la merce.",
    ),
    "jpn": ("店を開く前に商人用のポーションを使う。", "店を開いて商品を確認する。"),
    "kor": ("상점을 열기 전에 상인용 포션을 사용한다.", "상점을 열어 상품을 확인한다."),
    "pol": ("Użyj mikstury kupca przed otwarciem sklepu.", "Otwórz sklep, aby obejrzeć towary."),
    "ptb": (
        "Use a poção do mercador antes de abrir a loja.",
        "Abra a loja para examinar os produtos.",
    ),
    "rus": (
        "Использовать зелье для торговца до открытия магазина.",
        "Открыть магазин, чтобы осмотреть товары.",
    ),
    "spa": (
        "Usa la poción del mercader antes de abrir la tienda.",
        "Abre la tienda para revisar la mercancía.",
    ),
    "tha": ("ใช้ยาสำหรับพ่อค้าก่อนเปิดร้าน", "เปิดร้านเพื่อตรวจดูสินค้า"),
    "tur": ("Dükkânı açmadan önce tüccar iksirini kullan.", "Ürünleri incelemek için dükkânı aç."),
}


_MAP_REASONS = {
    "eng": "Only one reachable map destination is available.",
    "zhs": "当前只有一个可达的地图去向。",
    "zht": "目前只有一個可達的地圖去向。",
    "deu": "Es gibt nur ein erreichbares Kartenziel.",
    "esp": "Solo hay un destino accesible en el mapa.",
    "fra": "Une seule destination de la carte est accessible.",
    "ind": "Hanya ada satu tujuan peta yang dapat dicapai.",
    "ita": "C’è una sola destinazione raggiungibile sulla mappa.",
    "jpn": "到達できる地図上の行き先は一つだけ。",
    "kor": "지도에서 이동할 수 있는 목적지가 하나뿐이다.",
    "pol": "Na mapie jest tylko jeden dostępny cel.",
    "ptb": "Há apenas um destino acessível no mapa.",
    "rus": "На карте доступен только один путь.",
    "spa": "Solo hay un destino accesible en el mapa.",
    "tha": "มีจุดหมายบนแผนที่ที่ไปถึงได้เพียงแห่งเดียว",
    "tur": "Haritada ulaşılabilir tek bir hedef var.",
}
_CHEST_REASONS = {
    "eng": "Open the chest to reveal its contents.",
    "zhs": "打开宝箱查看其中的物品。",
    "zht": "打開寶箱查看其中的物品。",
    "deu": "Die Truhe öffnen, um ihren Inhalt zu sehen.",
    "esp": "Abre el cofre para ver su contenido.",
    "fra": "Ouvrir le coffre pour voir son contenu.",
    "ind": "Buka peti untuk melihat isinya.",
    "ita": "Apri il forziere per vederne il contenuto.",
    "jpn": "宝箱を開いて中身を確認する。",
    "kor": "상자를 열어 내용물을 확인한다.",
    "pol": "Otwórz skrzynię, aby zobaczyć jej zawartość.",
    "ptb": "Abra o baú para ver seu conteúdo.",
    "rus": "Открыть сундук, чтобы увидеть содержимое.",
    "spa": "Abre el cofre para ver su contenido.",
    "tha": "เปิดหีบเพื่อดูสิ่งของข้างใน",
    "tur": "İçindekileri görmek için sandığı aç.",
}
_CONTINUE_REASONS = {
    "eng": "No reward or available choice remains; continue.",
    "zhs": "没有待领取奖励或可用选项，继续前进。",
    "zht": "沒有待領取獎勵或可用選項，繼續前進。",
    "deu": "Keine Belohnung oder verfügbare Auswahl bleibt; weitergehen.",
    "esp": "No quedan recompensas ni opciones disponibles; continúa.",
    "fra": "Il ne reste ni récompense ni choix disponible ; continuer.",
    "ind": "Tidak ada hadiah atau pilihan yang tersedia; lanjutkan.",
    "ita": "Non restano ricompense né scelte disponibili; prosegui.",
    "jpn": "受け取る報酬も選べる項目もないため、先へ進む。",
    "kor": "남은 보상이나 선택 가능한 항목이 없으므로 계속 진행한다.",
    "pol": "Nie ma nagród ani dostępnych wyborów; kontynuuj.",
    "ptb": "Não restam recompensas nem escolhas disponíveis; prossiga.",
    "rus": "Наград и доступных вариантов нет; продолжить.",
    "spa": "No quedan recompensas ni opciones disponibles; continúa.",
    "tha": "ไม่มีรางวัลหรือทางเลือกที่ใช้ได้เหลืออยู่ จึงไปต่อ",
    "tur": "Ödül veya kullanılabilir seçenek kalmadı; devam et.",
}


def shop_controls(state: native.RunState) -> bool:
    operations = {action.operation for action in state.actions}
    return (
        native.ActionOperation.CLOSE_SHOP in operations
        or {native.ActionOperation.OPEN_SHOP, native.ActionOperation.LEAVE_SHOP} <= operations
    )


def _candidate(
    observation: native.Observation, shop_strategy: bool
) -> tuple[ActionStep, str, _Reason] | None:
    state = observation.state
    if (
        not isinstance(state, native.RunState)
        or state.control is not native.StateControl.READY
        or state.selection is not None
        or state.offers
    ):
        return None
    enabled = tuple(action for action in state.actions if action.enabled)
    if shop_controls(state):
        shop_operations = {
            native.ActionOperation.OPEN_SHOP,
            native.ActionOperation.CLOSE_SHOP,
            native.ActionOperation.LEAVE_SHOP,
        }
        if shop_strategy or any(action.operation not in shop_operations for action in enabled):
            return None
        opening = next(
            (action for action in enabled if action.operation is native.ActionOperation.OPEN_SHOP),
            None,
        )
        if opening is None:
            return None
        for ref in state.potions:
            potion = observation.objects[ref]
            if (
                isinstance(potion, native.PotionEntry)
                and potion.definition_id == "POTION.FOUL_POTION"
                and potion.usable is True
                and potion.needs_target is False
            ):
                return (
                    ActionStep(ActionKind.USE_POTION, ref),
                    f"{potion.name} ({potion.definition_id})",
                    _Reason.POTION,
                )
        return ActionStep(ActionKind.INTERACT, opening.ref), opening.label, _Reason.SHOP
    if state.screen is native.RunScreen.MAP:
        destinations = tuple(action for action in enabled if action.destination is not MISSING)
        if (
            observation.map is not MISSING
            and observation.map.legal_destinations is not None
            and len(observation.map.legal_destinations) == 1
            and len(destinations) == 1
            and destinations[0].destination == observation.map.legal_destinations[0]
        ):
            action = destinations[0]
            return ActionStep(ActionKind.INTERACT, action.ref), action.label, _Reason.MAP
    if state.screen is native.RunScreen.TREASURE:
        chests = tuple(
            action for action in enabled if action.operation is native.ActionOperation.OPEN_CHEST
        )
        if len(chests) == 1:
            action = chests[0]
            return ActionStep(ActionKind.INTERACT, action.ref), action.label, _Reason.CHEST
    if len(enabled) == 1 and enabled[0].operation is native.ActionOperation.PROCEED:
        action = enabled[0]
        return ActionStep(ActionKind.INTERACT, action.ref), action.label, _Reason.CONTINUE
    return None


def choose_auto(
    observation: native.Observation, *, shop_strategy: bool
) -> tuple[ActionProposal, AutomaticProgress] | None:
    language = observation.game_language
    shop_reasons = _SHOP_REASONS.get(language)
    if shop_reasons is None:
        return None
    candidate = _candidate(observation, shop_strategy)
    if candidate is None:
        return None
    step, label, kind = candidate
    reason = {
        _Reason.POTION: shop_reasons[0],
        _Reason.SHOP: shop_reasons[1],
        _Reason.MAP: _MAP_REASONS[language],
        _Reason.CHEST: _CHEST_REASONS[language],
        _Reason.CONTINUE: _CONTINUE_REASONS[language],
    }[kind]
    return ActionProposal((step,), reason), AutomaticProgress(label=label, reason=reason)
