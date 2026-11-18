namespace Sts2Mod.Core.Observation;

/// <summary>
/// Game-language fallback titles for icon-only native controls when the native control carries no text label.
/// Actual displayed native localization always takes precedence.
/// </summary>
public static class NativeInteractionLabels
{
    private sealed record Labels(
        string OpenShop,
        string CloseShop,
        string LeaveShop,
        string OpenChest,
        string Proceed,
        string Travel,
        string SoldOut,
        string CardRemovalService);

    public static string Operation(string language, InteractionOperation operation)
    {
        var labels = ForLanguage(language);
        return operation switch
        {
            InteractionOperation.OpenShop => labels?.OpenShop ?? "open_shop",
            InteractionOperation.CloseShop => labels?.CloseShop ?? "close_shop",
            InteractionOperation.LeaveShop => labels?.LeaveShop ?? "leave_shop",
            InteractionOperation.OpenChest => labels?.OpenChest ?? "open_chest",
            InteractionOperation.Proceed => labels?.Proceed ?? "proceed",
            _ => throw new ArgumentOutOfRangeException(nameof(operation)),
        };
    }

    public static string Travel(string language, string destination) =>
        ForLanguage(language)?.Travel ?? destination;

    public static string SoldOut(string language) =>
        ForLanguage(language)?.SoldOut ?? "sold_out";

    public static string CardRemovalService(string language) =>
        ForLanguage(language)?.CardRemovalService ?? "card_removal_service";

    private static Labels? ForLanguage(string language) => language switch
    {
        "eng" => new("Open shop", "Close shop", "Leave shop", "Open chest", "Continue", "Travel", "Sold out", "Card Removal Service"),
        "zhs" => new("打开商店", "关闭商店", "离开商店", "打开宝箱", "继续", "前往", "已售罄", "移除卡牌服务"),
        "zht" => new("開啟商店", "關閉商店", "離開商店", "打開寶箱", "繼續", "前往", "已售罄", "移除卡牌服務"),
        "deu" => new("Laden öffnen", "Laden schließen", "Laden verlassen", "Truhe öffnen", "Weiter", "Reisen", "Ausverkauft", "Kartenentfernungsdienst"),
        "esp" or "spa" => new("Abrir tienda", "Cerrar tienda", "Salir de la tienda", "Abrir cofre", "Continuar", "Viajar", "Agotado", "Servicio de eliminación de cartas"),
        "fra" => new("Ouvrir la boutique", "Fermer la boutique", "Quitter la boutique", "Ouvrir le coffre", "Continuer", "Voyager", "Épuisé", "Service de retrait de carte"),
        "ind" => new("Buka toko", "Tutup toko", "Tinggalkan toko", "Buka peti", "Lanjutkan", "Pergi", "Habis terjual", "Layanan Penghapusan Kartu"),
        "ita" => new("Apri negozio", "Chiudi negozio", "Lascia il negozio", "Apri forziere", "Continua", "Viaggia", "Esaurito", "Servizio di rimozione carte"),
        "jpn" => new("ショップを開く", "ショップを閉じる", "ショップを出る", "宝箱を開く", "続ける", "移動する", "売り切れ", "カード削除サービス"),
        "kor" => new("상점 열기", "상점 닫기", "상점 떠나기", "보물 상자 열기", "계속", "이동", "품절", "카드 제거 서비스"),
        "pol" => new("Otwórz sklep", "Zamknij sklep", "Opuść sklep", "Otwórz skrzynię", "Kontynuuj", "Podróżuj", "Wyprzedane", "Usługa usuwania kart"),
        "ptb" => new("Abrir loja", "Fechar loja", "Sair da loja", "Abrir baú", "Continuar", "Viajar", "Esgotado", "Serviço de Remoção de Cartas"),
        "rus" => new("Открыть магазин", "Закрыть магазин", "Покинуть магазин", "Открыть сундук", "Продолжить", "Перейти", "Продано", "Услуга удаления карт"),
        "tha" => new("เปิดร้านค้า", "ปิดร้านค้า", "ออกจากร้านค้า", "เปิดหีบ", "ดำเนินต่อ", "เดินทาง", "ขายหมดแล้ว", "บริการลบการ์ด"),
        "tur" => new("Dükkânı aç", "Dükkânı kapat", "Dükkândan ayrıl", "Sandığı aç", "Devam et", "Seyahat et", "Tükendi", "Kart Kaldırma Hizmeti"),
        _ => null,
    };
}
