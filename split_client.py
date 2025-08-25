from typing import Optional
from playwright.async_api import async_playwright
import os
import re

class SplitClient:
    """Грубый пример headless-скрипта для оформления покупки на split.tg.
    ⚠️ Сайт и селекторы могут меняться. Вам потребуется актуализировать селекторы под реальную разметку.
    """

    def __init__(self, email: str, password: str, *, headless: bool | None = None, slow_mo: int | None = None, record_video: bool = False):
        self.email = email
        self.password = password
        # Опции видимости/отладки (по умолчанию быстрый режим)
        self.headless = True if headless is None else bool(headless)
        self.slow_mo = 0 if slow_mo is None else int(slow_mo)
        self.record_video = bool(record_video)

    async def buy_stars(self, tg_username: str, qty: int, *, asset_preference: str = "TON") -> str:
        """Покупает qty звёзд на пользователя tg_username. Возвращает id заказа/квитанции.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
            ctx_kwargs = {}
            if self.record_video:
                os.makedirs("videos", exist_ok=True)
                ctx_kwargs["record_video_dir"] = "videos"
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            # Увеличим таймауты по-умолчанию, чтобы не спешить
            try:
                page.set_default_timeout(90000)
            except Exception:
                pass

            # 1) Открыть сайт
            await page.goto("https://split.tg/stars", wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass

            # Переход в раздел "Premium & Stars" (SPA: без обязательной навигации)
            opened_store = False
            try:
                link = page.locator("a[href='/store'][data-discover='true']")
                await link.wait_for(timeout=10000)
                await link.scroll_into_view_if_needed(timeout=2000)
                await link.click()
                opened_store = True
            except Exception:
                premium_selectors = [
                    "a[href='/store']",
                    "a:has-text('Premium & Stars')",
                    "button:has-text('Premium & Stars')",
                    "[role=tab]:has-text('Premium & Stars')",
                    "text=Premium & Stars",
                ]
                for sel in premium_selectors:
                    try:
                        el = page.locator(sel)
                        await el.wait_for(timeout=5000)
                        try:
                            await el.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        await el.click()
                        opened_store = True
                        break
                    except Exception:
                        continue

            # Ждём смены URL/контента магазина
            try:
                await page.wait_for_url("**/store*", timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass

            # Переходим в карточку товара Telegram Stars, если на /store список товаров
            product_opened = False
            product_candidates = [
                "a[href*='/stars']",
                "a:has-text('Telegram Stars')",
                "button:has-text('Telegram Stars')",
                "text=Telegram Stars",
            ]
            for sel in product_candidates:
                try:
                    el = page.locator(sel)
                    await el.wait_for(timeout=5000)
                    try:
                        await el.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await el.click()
                    product_opened = True
                    try:
                        await page.wait_for_url("**/stars*", timeout=10000)
                    except Exception:
                        pass
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    break
                except Exception:
                    continue
            if product_opened:
                try:
                    await page.wait_for_load_state("networkidle", timeout=60000)
                except Exception:
                    pass

            # Нажимаем кнопку/ссылку "Buy Stars to User" (может быть <a> или <button> со вложенным <span>)
            buy_to_user_clicked = False
            buy_to_user_locators = [
                page.get_by_role("button", name="Buy Stars to User"),
                page.locator("a:has(span:has-text('Buy Stars to User'))"),
                page.locator("button:has(span:has-text('Buy Stars to User'))"),
                page.get_by_text("Buy Stars to User", exact=True),
                page.locator("xpath=//*[self::a or self::button][.//span[contains(normalize-space(.), 'Buy Stars to User')]]"),
            ]
            for loc in buy_to_user_locators:
                try:
                    await loc.wait_for(timeout=15000)
                    try:
                        await loc.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await loc.click()
                    buy_to_user_clicked = True
                    break
                except Exception:
                    continue
            if not buy_to_user_clicked:
                try:
                    await page.screenshot(path="split_debug_no_buy_to_user.png")
                except Exception:
                    pass
                # некоторые страницы сразу показывают форму без этой кнопки — продолжаем

            # после клика подождём дорендер формы
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            async def _accept_cookies():
                # Пытаемся закрыть баннер согласия с cookies, если он мешает кликам
                candidates = [
                    "button:has-text('Accept')",
                    "button:has-text('I agree')",
                    "button:has-text('Я согласен')",
                    "button:has-text('Принять')",
                    "text=Accept",
                    "text=Принять",
                ]
                for sel in candidates:
                    try:
                        el = page.locator(sel)
                        await el.wait_for(timeout=2000)
                        try:
                            await el.scroll_into_view_if_needed(timeout=1000)
                        except Exception:
                            pass
                        await el.click()
                        break
                    except Exception:
                        continue

            async def _click_first(frames, builders_or_factory, wait_timeout=60000):
                for fr in frames:
                    try:
                        if callable(builders_or_factory):
                            locators = builders_or_factory(fr)
                        else:
                            tmp = builders_or_factory
                            locators = []
                            for b in tmp:
                                try:
                                    locators.append(b(fr) if callable(b) else b)
                                except Exception:
                                    continue
                    except Exception:
                        continue

                    for loc in locators:
                        try:
                            await loc.wait_for(timeout=wait_timeout)
                            try:
                                await loc.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            await loc.click()
                            return True
                        except Exception:
                            continue
                return False

            async def _all_frames():
                # return main frame first, then children
                frs = [page.main_frame]
                try:
                    for f in page.frames:
                        if f not in frs:
                            frs.append(f)
                except Exception:
                    pass
                return frs

            async def _fill_first(frames, builders_or_factory, value, wait_timeout=60000, type_delay=20):
                for fr in frames:
                    # Получаем список локаторов для текущего фрейма:
                    try:
                        if callable(builders_or_factory):
                            # Фабрика возвращает уже список локаторов для данного фрейма
                            locators = builders_or_factory(fr)
                        else:
                            # Список builder'ов или уже готовых локаторов
                            tmp = builders_or_factory
                            locators = []
                            for b in tmp:
                                try:
                                    locators.append(b(fr) if callable(b) else b)
                                except Exception:
                                    continue
                    except Exception:
                        continue

                    # Перебираем локаторы и пытаемся заполнить
                    for loc in locators:
                        try:
                            await loc.wait_for(timeout=wait_timeout)
                            try:
                                await loc.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            await loc.click()
                            try:
                                await loc.fill("")
                            except Exception:
                                pass
                            await loc.type(value, delay=type_delay)
                            return True
                        except Exception:
                            continue
                return False

            await _accept_cookies()

            # === Username field: сначала точный placeholder, затем fallback ===
            user_value = tg_username.lstrip("@")
            username_filled = False
            # 1) Прямая попытка по точному placeholder из вашей вёрстки
            try:
                u = page.get_by_placeholder("Enter Telegram @username")
                await u.wait_for(timeout=15000)
                await u.scroll_into_view_if_needed(timeout=2000)
                await u.click()
                try:
                    await u.fill("")
                except Exception:
                    pass
                await u.type(user_value, delay=20)
                username_filled = True
            except Exception:
                username_filled = False

            # 2) Fallback: поиск во всех фреймах по нескольким локаторам (RU/EN/attr)
            ok_user = False
            if not username_filled:
                frames = await _all_frames()
                def _user_locators(fr):
                    return [
                        fr.get_by_placeholder("Введите Telegram @username"),
                        fr.get_by_placeholder("Enter Telegram @username"),
                        fr.locator("input[placeholder*='@username']"),
                        fr.locator("input[name='username']"),
                        fr.locator("input[type='text']").first,
                    ]
                ok_user = await _fill_first(frames, _user_locators, user_value, wait_timeout=60000, type_delay=20)
            else:
                ok_user = True

            # 3) Доп. fallback: попытаться в первый input
            if not ok_user:
                try:
                    first_input = page.locator("input").first
                    await first_input.wait_for(timeout=5000)
                    await first_input.click()
                    await first_input.type(user_value, delay=20)
                    ok_user = True
                except Exception:
                    ok_user = False

            # 4) Если так и не нашли — снимем скрин и дамп HTML
            if not ok_user:
                try:
                    await page.screenshot(path="split_debug_no_username.png")
                    html = await page.content()
                    with open("split_debug_no_username.html", "w", encoding="utf-8") as f:
                        f.write(html)
                except Exception:
                    pass
                raise RuntimeError("Не найдено поле ввода username (см. split_debug_no_username.png / .html)")

            # === Currency dropdown — жёстко: сначала кликаем по кнопке USDT (TON), затем выбираем TON ===
            # 1) Открыть дропдаун валют (строго тот <button> из макета)
            dropdown_opened = False
            dropdown_precise_locators = [
                # XPath по точному дереву: кнопка, внутри div с текстом 'USDT (TON)'
                page.locator("xpath=//button[.//div[contains(normalize-space(.), 'USDT (TON)')]]"),
                # Текстовый селектор через has() — более устойчивый к классам Tailwind
                page.locator("button:has(div:has-text('USDT (TON)'))"),
                # Роль + текст — запасной вариант
                page.get_by_role("button", name=re.compile(r"USDT\s*\(TON\)", re.I)),
            ]
            for loc in dropdown_precise_locators:
                try:
                    await loc.wait_for(timeout=10000)
                    try:
                        await loc.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await loc.click()
                    dropdown_opened = True
                    break
                except Exception:
                    continue

            # Если не вышло — пробуем прежние универсальные варианты
            if not dropdown_opened:
                fallback_dropdown = [
                    page.get_by_role("button", name=re.compile(r"(USDT|TON)", re.I)),
                    page.locator("button.rounded-xl:has(div)").first,
                ]
                for loc in fallback_dropdown:
                    try:
                        await loc.wait_for(timeout=8000)
                        try:
                            await loc.scroll_into_view_if_needed(timeout=1000)
                        except Exception:
                            pass
                        await loc.click()
                        dropdown_opened = True
                        break
                    except Exception:
                        continue

            # 2) Выбрать пункт TON в меню
            if dropdown_opened:
                ton_locators = [
                    page.locator("li.flex.cursor-pointer:has-text('TON')"),
                    page.locator("li:has-text('TON')"),
                    page.get_by_role("listitem", name=re.compile(r"\\bTON\\b", re.I)),
                    page.locator("xpath=//li[.//text()[contains(., 'TON')]]"),
                ]
                ton_clicked = False
                for opt in ton_locators:
                    try:
                        await opt.wait_for(timeout=10000)
                        try:
                            await opt.scroll_into_view_if_needed(timeout=1000)
                        except Exception:
                            pass
                        await opt.click()
                        ton_clicked = True
                        break
                    except Exception:
                        continue
                if not ton_clicked:
                    try:
                        await page.screenshot(path="split_debug_no_asset_TON.png")
                    except Exception:
                        pass
            # если дропдаун не открылся — возможно нужная валюта уже выбрана, продолжаем

            # === Amount field: расширенный поиск и ввод количества ===
            async def _try_type_amount(loc, value: str, wait_timeout=15000):
                try:
                    await loc.wait_for(timeout=wait_timeout)
                    try:
                        await loc.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await loc.click()
                    try:
                        await loc.fill("")
                    except Exception:
                        pass
                    await loc.type(value, delay=10)
                    return True
                except Exception:
                    return False

            qty_str = str(qty)
            ok_amount = False

            # 1) Плейсхолдеры (RU/EN, regex)
            for loc in [
                page.get_by_placeholder("Введите кол-во Telegram Stars"),
                page.get_by_placeholder("Enter number of Telegram Stars"),
                page.get_by_placeholder(re.compile(r"(amount|Quantity|кол-во|количество|Stars)", re.I)),
                page.locator("input[placeholder*='Stars']"),
                page.locator("input[placeholder*='кол']"),
            ]:
                if await _try_type_amount(loc, qty_str):
                    ok_amount = True
                    break

            # 2) По label / aria-label
            if not ok_amount:
                for loc in [
                    page.get_by_label(re.compile(r"(amount|Quantity|кол-во|количество|Stars)", re.I)),
                    page.locator("input[aria-label*='Stars']"),
                    page.locator("input[aria-label*='Количество']"),
                ]:
                    if await _try_type_amount(loc, qty_str):
                        ok_amount = True
                        break

            # 3) Относительно текста рядом (XPath: ближайший input после текста)
            if not ok_amount:
                near_xpaths = [
                    "xpath=(//label[contains(., 'Stars')]/following::input)[1]",
                    "xpath=(//span[contains(., 'Stars')]/following::input)[1]",
                    "xpath=(//*[contains(., 'Количество')]/following::input)[1]",
                ]
                for xp in near_xpaths:
                    if await _try_type_amount(page.locator(xp), qty_str):
                        ok_amount = True
                        break

            # 4) Имя/тип
            if not ok_amount:
                for loc in [
                    page.locator("input[name='amount']"),
                    page.locator("input[name='qty']"),
                    page.locator("input[type='number']").first,
                ]:
                    if await _try_type_amount(loc, qty_str):
                        ok_amount = True
                        break

            # 5) Попытка во всех фреймах через универсальные локаторы (на случай вложенных компонентов)
            if not ok_amount:
                frames = await _all_frames()
                def _amount_locators(fr):
                    return [
                        fr.get_by_placeholder("Введите кол-во Telegram Stars"),
                        fr.get_by_placeholder("Enter number of Telegram Stars"),
                        fr.get_by_placeholder(re.compile(r"(amount|Quantity|кол-во|количество|Stars)", re.I)),
                        fr.locator("input[name='amount']"),
                        fr.locator("input[name='qty']"),
                        fr.locator("input[type='number']").first,
                    ]
                ok_amount = await _fill_first(frames, _amount_locators, qty_str, wait_timeout=30000, type_delay=10)

            # 6) Если всё ещё не нашли — снимем подробные дампы, чтобы быстро подобрать нужный селектор
            if not ok_amount:
                try:
                    await page.screenshot(path="split_debug_no_amount.png")
                    html = await page.content()
                    with open("split_debug_no_amount.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    # Дополнительно соберём список всех видимых input и их атрибутов
                    try:
                        inputs = page.locator("input:visible")
                        cnt = await inputs.count()
                        lines = []
                        for i in range(cnt):
                            el = inputs.nth(i)
                            try:
                                ph = await el.get_attribute("placeholder")
                            except Exception:
                                ph = None
                            try:
                                nm = await el.get_attribute("name")
                            except Exception:
                                nm = None
                            try:
                                typ = await el.get_attribute("type")
                            except Exception:
                                typ = None
                            try:
                                aria = await el.get_attribute("aria-label")
                            except Exception:
                                aria = None
                            lines.append(f"#{i}: type={typ} name={nm} placeholder={ph} aria-label={aria}")
                        with open("split_inputs_dump.txt", "w", encoding="utf-8") as f:
                            f.write("\n".join(lines))
                    except Exception:
                        pass
                except Exception:
                    pass
                raise RuntimeError("Не найдено поле количества (см. split_debug_no_amount.png / .html / split_inputs_dump.txt)")

            # 5) Подтвердить заказ. Пробуем несколько вариантов кнопки (RU/EN)
            buy_selectors = [
                "button:has-text('Купить Telegram Stars')",
                "button:has-text('Buy Telegram Stars')",
                "button:has-text('Оплатить')",
                "button:has-text('Buy')",
                "button[type='submit']",
            ]

            await _accept_cookies()

            clicked = False
            for sel in buy_selectors:
                try:
                    el = page.locator(sel)
                    await el.wait_for(timeout=60000)
                    try:
                        await el.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                # Попробуем отправить форму по Enter
                try:
                    await page.keyboard.press("Enter")
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                try:
                    await page.screenshot(path="split_debug_no_buy.png")
                except Exception:
                    pass
                raise RuntimeError("Кнопка покупки не найдена. Снял скриншот split_debug_no_buy.png")

            # 6) Пытаемся понять, что произошло: либо появился номер заказа, либо отдали ссылку на оплату (CryptoBot)
            order_id = None
            invoice_url = None
            try:
                # Вариант 1: появился блок с номером заказа
                await page.wait_for_selector(".order-id, text=Order ID, text=Номер заказа", timeout=30000)
                # Считываем любой из возможных элементов
                for sel in [".order-id", "text=Order ID", "text=Номер заказа"]:
                    try:
                        txt = await page.text_content(sel)
                        if txt:
                            order_id = txt.strip()
                            break
                    except Exception:
                        pass
            except Exception:
                # Вариант 2: страница показала ссылку на оплату через CryptoBot — найдём t.me/CryptoBot
                links = await page.locator("a").all()
                for i in range(len(links)):
                    try:
                        href = await links[i].get_attribute("href")
                    except Exception:
                        href = None
                    if href and ("t.me/CryptoBot" in href or "t.me/CryptoBot/app" in href or "startapp=invoice-" in href):
                        invoice_url = href
                        break

            try:
                html = await page.content()
                with open("split_step_2_result.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

            await context.close()
            await browser.close()

        # Возвращаем order_id если он есть, иначе специальный маркер с ссылкой оплаты
        if order_id:
            return order_id
        return f"PAYMENT_LINK::{invoice_url}"