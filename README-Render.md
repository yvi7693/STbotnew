# Развёртывание на Render.com

## Файлы
- `requirements.txt` — зависимости.
- `Procfile` — указывает Render, что это worker-процесс (`python bot.py`).
- `runtime.txt` — версия Python.
- `render.yaml` — инфраструктура как код (создаёт воркер и ставит браузер Playwright).

## Шаги
1. Залей репозиторий на GitHub с файлами проекта и этими четырьмя файлами.
2. На Render: **New +** → **Blueprint** → укажи репозиторий → Render прочитает `render.yaml` и создаст сервис.
3. В сервисе на вкладке **Environment** добавь переменные:
   - `BOT_TOKEN`
   - `ADMIN_IDS` (через запятую, например `123,456`)
   - `CRYPTOPAY_API_TOKEN`
   - `PAYMENT_PROVIDER_TOKEN` (если используешь)
   - `SPLIT_EMAIL`, `SPLIT_PASSWORD`
   - При желании поменяй `USER_PRICE_PER_STAR`, `COST_PER_STAR`, `SBP_INSTRUCTION`, ссылки `CRYPTO_TON_LINK`, `CRYPTO_USDT_LINK`.
4. Нажми **Deploy**.

### Заметки по Playwright
Сервис использует `python -m playwright install --with-deps chromium`, чтобы поставить браузер и зависимости во время сборки. 
Если увидишь ошибки, проверь логи сборки. Иногда помогает повторный деплой.
