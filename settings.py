import os
from dotenv import load_dotenv

load_dotenv()

CRYPTOPAY_API_TOKEN = os.getenv("CRYPTOPAY_API_TOKEN", "").strip()
CRYPTOPAY_API_URL = os.getenv("CRYPTOPAY_API_URL", "https://pay.crypt.bot/api").strip()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")  # для Stars не обязателен, используем XTR
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}

# Базовые цены (можно менять командами в боте)
USER_PRICE_PER_STAR = float(os.getenv("USER_PRICE_PER_STAR", "1.60"))  # цена для клиента в Звёздах? см. ниже
COST_PER_STAR = float(os.getenv("COST_PER_STAR", "1.45"))  # ваша себестоимость (по split.tg)

# ---- RUB pricing (for internal RUB balance) ----
USER_PRICE_PER_STAR_RUB = float(os.getenv("USER_PRICE_PER_STAR_RUB", "3.50"))
COST_PER_STAR_RUB = float(os.getenv("COST_PER_STAR_RUB", "3.10"))

# Payment method instructions/links (fill with your real data)
SBP_INSTRUCTION = os.getenv(
    "SBP_INSTRUCTION",
    "Отсканируйте QR СБП или переведите по реквизитам. После оплаты нажмите 'Я оплатил'.",
)
CRYPTO_TON_LINK = os.getenv("CRYPTO_TON_LINK", "")
CRYPTO_USDT_LINK = os.getenv("CRYPTO_USDT_LINK", "")

# ВНИМАНИЕ: В Stars все цены должны указываться в XTR (1 Star = 1 XTR-единица).
# Для удобства ниже предполагаем, что USER_PRICE_PER_STAR и COST_PER_STAR выражены в Stars за 1 Star = 1.
# Если вы хотите мыслить в рублях — храните курс отдельно и конвертируйте.

SPLIT_EMAIL = os.getenv("SPLIT_EMAIL", "")
SPLIT_PASSWORD = os.getenv("SPLIT_PASSWORD", "")