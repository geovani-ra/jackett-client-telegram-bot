import html
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

JACKETT_URL = os.environ.get(
    "JACKETT_URL",
    "http://jackett:9117",
).rstrip("/")

JACKETT_API_KEY = os.environ["JACKETT_API_KEY"]

TELEGRAM_ALLOWED_USER_IDS = {
    int(user_id.strip())
    for user_id in os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",")
    if user_id.strip()
}

JACKETT_SEARCH_URL = f"{JACKETT_URL}/api/v2.0/indexers/all/results/torznab/"

JACKETT_HEALTH_URL = f"{JACKETT_URL}/health"

# ---------------------------------------------------------------------------
# Limites
# ---------------------------------------------------------------------------

RESULTS_PER_PAGE = 10

# Quantos resultados buscar no Jackett de uma vez só. A paginação
# depois é feita localmente (fatiando essa lista), sem nova consulta
# ao Jackett — o parâmetro "offset" do Torznab não é confiável quando
# o indexador é "all", pois muitos trackers o ignoram.
SEARCH_RESULT_LIMIT = int(os.environ.get("SEARCH_RESULT_LIMIT", "50"))

SEARCH_TIMEOUT = 60.0
HEALTH_TIMEOUT = 10.0
TORRENT_TIMEOUT = 60.0

# Segurança: não aceitar arquivos grandes como torrent.
MAX_TORRENT_SIZE = 20 * 1024 * 1024

# Tamanho de cada pedaço lido via streaming ao baixar um torrent.
# Mantém o uso de memória baixo (importante numa VPS com pouca RAM).
DOWNLOAD_CHUNK_SIZE = 64 * 1024

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("telegram-bot")


# ---------------------------------------------------------------------------
# Autorização
# ---------------------------------------------------------------------------


def authorized(update: Update) -> bool:
    user = update.effective_user

    if user is None:
        return False

    if user.id not in TELEGRAM_ALLOWED_USER_IDS:
        logger.warning(
            "Usuário não autorizado: user_id=%s username=%s",
            user.id,
            user.username,
        )
        return False

    return True


def authorized_user_id(user_id: int) -> bool:
    return user_id in TELEGRAM_ALLOWED_USER_IDS


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------


def format_size(value: str | None) -> str:
    if not value:
        return "?"

    try:
        size = float(value)
        units = ["B", "KB", "MB", "GB", "TB"]

        for unit in units:
            if size < 1024:
                return f"{size:.1f} {unit}"

            size /= 1024

        return f"{size:.1f} PB"

    except (ValueError, TypeError):
        return value


def extract_attr(
    item: ET.Element,
    name: str,
) -> str | None:
    for child in item:
        if not child.tag.endswith("attr"):
            continue

        if child.attrib.get("name") == name:
            return child.attrib.get("value")

    return None


def extract_indexer(item: ET.Element) -> str:
    element = item.find("jackettindexer")

    if element is not None and element.text:
        return element.text.strip()

    return "Desconhecido"


def sanitize_filename(name: str) -> str:
    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    name = name.strip().strip(".")

    if not name:
        name = "download"

    return name[:180]


def safe_int(value: str | None) -> int:
    """
    Converte para int com segurança, tratando None/valores inválidos
    como 0. Usado para ordenar resultados por seeders.
    """

    if value is None:
        return 0

    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def normalize_magnet(value: str) -> str:
    """
    Normaliza casos como:
        magnet://?xt=...
    para:
        magnet:?xt=...
    """

    value = value.strip()

    if value.startswith("magnet://"):
        return "magnet:" + value[len("magnet://") :]

    return value


def looks_like_torrent(content: bytes) -> bool:
    """
    Validação leve e heurística de um arquivo .torrent (bencode).

    Não faz parsing completo do bencode (evita gastar CPU/memória à
    toa numa VPS pequena), mas rejeita com segurança qualquer coisa
    que claramente não seja um torrent: HTML disfarçado, JSON de erro,
    arquivo truncado, texto qualquer, etc.

    Um .torrent válido é um dicionário bencode: começa com "d",
    termina com "e" e contém a chave obrigatória "info".
    """

    if len(content) < 20:
        return False

    if not content.startswith(b"d"):
        return False

    if not content.rstrip().endswith(b"e"):
        return False

    if b"4:info" not in content:
        return False

    return True


def safe_url_for_log(url: str) -> str:
    """
    Remove query string e fragmento dos logs para não registrar
    a API key do Jackett.
    """

    try:
        parsed = urlparse(url)

        if not parsed.scheme or not parsed.netloc:
            return "<url inválida>"

        path = parsed.path or "/"

        return f"{parsed.scheme}://{parsed.netloc}{path}"

    except Exception:
        return "<url inválida>"


# ---------------------------------------------------------------------------
# Parser Torznab
# ---------------------------------------------------------------------------


def parse_results(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)

    results = []

    for item in root.findall(".//item"):
        title = item.findtext("title") or "Sem título"
        guid = item.findtext("guid") or ""
        link = item.findtext("link") or ""
        comments = item.findtext("comments") or ""

        size = item.findtext("size")

        if not size:
            size = extract_attr(item, "size")

        seeders = extract_attr(item, "seeders")
        peers = extract_attr(item, "peers")
        leechers = extract_attr(item, "leechers")
        infohash = extract_attr(item, "infohash")
        magnet = extract_attr(item, "magneturl")
        indexer = extract_indexer(item)

        if magnet:
            magnet = normalize_magnet(magnet)

        # Algumas fontes podem colocar o magnet diretamente no link.
        if not magnet and link.startswith(("magnet:", "magnet://")):
            magnet = normalize_magnet(link)
            link = ""

        results.append(
            {
                "title": title,
                "guid": guid,
                "download_url": link,
                "details_url": comments,
                "magnet": magnet,
                "size": size,
                "seeders": seeders,
                "peers": peers,
                "leechers": leechers,
                "infohash": infohash,
                "indexer": indexer,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Jackett - Busca
# ---------------------------------------------------------------------------


async def search_jackett(
    search_query: str,
) -> list[dict]:
    """
    Busca no Jackett uma única vez, trazendo até SEARCH_RESULT_LIMIT
    resultados. A paginação exibida ao usuário é feita depois, em
    memória, fatiando essa lista — não fazemos uma nova consulta ao
    Jackett a cada página (ver comentário de SEARCH_RESULT_LIMIT).
    """

    params = {
        "apikey": JACKETT_API_KEY,
        "t": "search",
        "q": search_query,
        "limit": SEARCH_RESULT_LIMIT,
    }

    async with httpx.AsyncClient(
        timeout=SEARCH_TIMEOUT,
        follow_redirects=True,
    ) as client:
        response = await client.get(
            JACKETT_SEARCH_URL,
            params=params,
        )

    response.raise_for_status()

    results = parse_results(response.text)

    # Ordena por seeders (maior primeiro). Sort estável: resultados
    # com o mesmo número de seeders mantêm a ordem de relevância
    # original devolvida pelo Jackett.
    results.sort(
        key=lambda result: safe_int(result.get("seeders")),
        reverse=True,
    )

    return results


# ---------------------------------------------------------------------------
# Jackett - Health
# ---------------------------------------------------------------------------


async def check_jackett_health() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(
            timeout=HEALTH_TIMEOUT,
        ) as client:
            response = await client.get(
                JACKETT_HEALTH_URL,
            )

        response.raise_for_status()

        return True, response.text.strip()

    except httpx.TimeoutException:
        return False, "Timeout ao conectar ao Jackett."

    except httpx.ConnectError:
        return False, "Não foi possível conectar ao Jackett."

    except httpx.HTTPStatusError as exc:
        return (
            False,
            f"Jackett respondeu HTTP {exc.response.status_code}.",
        )

    except Exception:
        logger.exception("Erro no health check do Jackett")

        return (
            False,
            "Erro inesperado ao consultar o Jackett.",
        )


# ---------------------------------------------------------------------------
# Download do torrent
# ---------------------------------------------------------------------------


async def download_torrent(
    url: str,
    destination: str,
) -> tuple[int, str | None]:
    """
    Acessa a URL /dl do Jackett e trata manualmente os redirects.

    Retorna:

        (bytes, None)
            Arquivo torrent baixado.

        (0, magnet)
            O indexador redirecionou para um magnet.

    O parâmetro follow_redirects é propositalmente False:
    precisamos interceptar redirects para magnet://.
    """

    if not url.startswith(("http://", "https://")):
        raise ValueError("A URL de download não é HTTP/HTTPS.")

    current_url = url

    async with httpx.AsyncClient(
        timeout=TORRENT_TIMEOUT,
        follow_redirects=False,
    ) as client:
        for redirect_count in range(10):
            logger.info(
                "Acessando download: %s",
                safe_url_for_log(current_url),
            )

            async with client.stream(
                "GET",
                current_url,
            ) as response:
                logger.info(
                    "Resposta download: status=%s content-type=%s content-length=%s",
                    response.status_code,
                    response.headers.get("content-type"),
                    response.headers.get("content-length"),
                )

                # ---------------------------------------------------
                # Redirect HTTP
                # ---------------------------------------------------

                if response.status_code in {
                    301,
                    302,
                    303,
                    307,
                    308,
                }:
                    location = response.headers.get("location")

                    if not location:
                        raise ValueError(
                            "O servidor retornou redirecionamento sem destino."
                        )

                    location = location.strip()

                    logger.info(
                        "Redirect recebido para protocolo %s",
                        location.split(":", 1)[0],
                    )

                    # -----------------------------------------------
                    # Redirect para magnet
                    # -----------------------------------------------

                    if location.startswith(("magnet:", "magnet://")):
                        magnet = normalize_magnet(location)

                        return 0, magnet

                    # -----------------------------------------------
                    # Redirect HTTP/HTTPS
                    # -----------------------------------------------

                    if location.startswith(("http://", "https://")):
                        current_url = location
                        continue

                    # -----------------------------------------------
                    # Redirect relativo
                    # -----------------------------------------------

                    current_url = urljoin(
                        current_url,
                        location,
                    )

                    parsed = urlparse(current_url)

                    if parsed.scheme in {
                        "magnet",
                    }:
                        return 0, normalize_magnet(current_url)

                    if parsed.scheme not in {
                        "http",
                        "https",
                    }:
                        raise ValueError(
                            "Redirecionamento para protocolo não suportado."
                        )

                    continue

                # ---------------------------------------------------
                # Resposta final
                # ---------------------------------------------------

                response.raise_for_status()

                content_type = (response.headers.get("content-type") or "").lower()

                # ---------------------------------------------------
                # Corte adiantado pelo Content-Length, se presente.
                # Evita começar a ler o corpo se o servidor já avisou
                # que o arquivo é maior que o permitido.
                # ---------------------------------------------------

                declared_size = None

                content_length_header = response.headers.get("content-length")

                if content_length_header:
                    try:
                        declared_size = int(content_length_header)
                    except ValueError:
                        declared_size = None

                if declared_size is not None and declared_size > MAX_TORRENT_SIZE:
                    raise ValueError(
                        "O arquivo declarado "
                        f"({format_size(str(declared_size))}) excede "
                        f"o limite de {format_size(str(MAX_TORRENT_SIZE))}."
                    )

                # ---------------------------------------------------
                # Leitura em streaming, cortando assim que o total
                # ultrapassar o limite — nunca mantemos mais que
                # ~MAX_TORRENT_SIZE bytes na memória do processo.
                # ---------------------------------------------------

                chunks = bytearray()

                async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_SIZE):
                    chunks.extend(chunk)

                    if len(chunks) > MAX_TORRENT_SIZE:
                        raise ValueError(
                            "O arquivo recebido excede o limite "
                            f"permitido de "
                            f"{format_size(str(MAX_TORRENT_SIZE))}."
                        )

                content = bytes(chunks)

            logger.info(
                "Resposta final: content-type=%s bytes=%d",
                content_type,
                len(content),
            )

            # -----------------------------------------------------------
            # Proteção contra resposta HTML
            # -----------------------------------------------------------

            stripped = content.lstrip()

            if (
                "text/html" in content_type
                or stripped.startswith(b"<!DOCTYPE html")
                or stripped.startswith(b"<html")
            ):
                raise ValueError(
                    "O indexador retornou uma página HTML em vez de um arquivo torrent."
                )

            # -----------------------------------------------------------
            # Caso o servidor retorne magnet como texto
            # -----------------------------------------------------------

            try:
                text = content.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()
            except Exception:
                text = ""

            if text.startswith(("magnet:", "magnet://")):
                return 0, normalize_magnet(text)

            # -----------------------------------------------------------
            # Arquivo vazio
            # -----------------------------------------------------------

            if not content:
                raise ValueError("O servidor respondeu sem conteúdo.")

            # -----------------------------------------------------------
            # Validação básica: precisa parecer um torrent de verdade.
            # -----------------------------------------------------------

            if not looks_like_torrent(content):
                raise ValueError("O arquivo recebido não parece ser um torrent válido.")

            # -----------------------------------------------------------
            # Salva arquivo
            # -----------------------------------------------------------

            with open(
                destination,
                "wb",
            ) as file:
                file.write(content)

            return len(content), None

        raise ValueError("Número máximo de redirecionamentos excedido.")


# ---------------------------------------------------------------------------
# Formatação dos resultados
# ---------------------------------------------------------------------------


def format_result(
    index: int,
    result: dict,
) -> str:

    title = html.escape(result["title"])

    size = format_size(result["size"])

    seeders = result["seeders"] or "?"
    peers = result["peers"] or "?"
    leechers = result["leechers"] or "?"

    indexer = html.escape(result["indexer"] or "Desconhecido")

    lines = [
        f"<b>{index}. {title}</b>",
        f"📦 {size} | ⬆️ {seeders}",
        f"⬇️ {leechers} | 👥 {peers} | 🌐 {indexer}",
    ]

    if result.get("infohash"):
        lines.append(f"🔑 <code>{html.escape(result['infohash'])}</code>")

    return "\n".join(lines)


def build_result_keyboard(
    results: list[dict],
    start_index: int = 0,
) -> InlineKeyboardMarkup:
    """
    'results' é a fatia (página) atual. 'start_index' é a posição
    dessa fatia dentro da lista completa armazenada em
    context.user_data["search_results"] — é isso que vai no
    callback_data, para que magnet_callback/torrent_callback
    encontrem o item certo independente da página exibida.
    """

    keyboard = []

    for local_index, result in enumerate(results):
        index = start_index + local_index

        row = []

        if result.get("magnet"):
            row.append(
                InlineKeyboardButton(
                    f"📋 Magnet #{index + 1}",
                    callback_data=f"magnet:{index}",
                )
            )

        if result.get("download_url"):
            row.append(
                InlineKeyboardButton(
                    f"📥 Torrent #{index + 1}",
                    callback_data=f"torrent:{index}",
                )
            )

        if result.get("details_url"):
            details_url = result["details_url"]

            if details_url.startswith(("http://", "https://")):
                row.append(
                    InlineKeyboardButton(
                        f"🌐 Detalhes #{index + 1}",
                        url=details_url,
                    )
                )

        if row:
            keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def build_pagination_keyboard(
    page: int,
    has_next: bool,
) -> InlineKeyboardMarkup:

    row = []

    if page > 0:
        row.append(
            InlineKeyboardButton(
                "⬅️ Anterior",
                callback_data=f"page:{page - 1}",
            )
        )

    if has_next:
        row.append(
            InlineKeyboardButton(
                "Próxima ➡️",
                callback_data=f"page:{page + 1}",
            )
        )

    if not row:
        return InlineKeyboardMarkup([])

    return InlineKeyboardMarkup([row])


def combine_keyboards(
    result_keyboard: InlineKeyboardMarkup,
    pagination_keyboard: InlineKeyboardMarkup,
) -> InlineKeyboardMarkup:

    rows = list(result_keyboard.inline_keyboard)

    rows.extend(pagination_keyboard.inline_keyboard)

    return InlineKeyboardMarkup(rows)


def build_results_message(
    search_query: str,
    results: list[dict],
    page: int,
    total: int,
) -> str:

    first_result = page * RESULTS_PER_PAGE + 1

    last_result = first_result + len(results) - 1

    parts = [
        f"🔎 <b>{html.escape(search_query)}</b>",
        f"Resultados {first_result}–{last_result} de {total}",
        "",
    ]

    for index, result in enumerate(
        results,
        start=first_result,
    ):
        parts.append(
            format_result(
                index,
                result,
            )
        )

        parts.append("")

    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not authorized(update):
        return

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "Bot conectado.\n\n"
        "Comandos disponíveis:\n"
        "/search <termo> — pesquisar no Jackett\n"
        "/ping — testar conexão com o Jackett\n"
        "/help — mostrar ajuda"
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not authorized(update):
        return

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "Comandos disponíveis:\n\n"
        "/search <termo> — pesquisar no Jackett\n"
        "/ping — testar conexão com o Jackett\n"
        "/help — mostrar esta mensagem"
    )


# ---------------------------------------------------------------------------
# /ping
# ---------------------------------------------------------------------------


async def ping(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not authorized(update):
        return

    message = update.effective_message

    if message is None:
        return

    status_message = await message.reply_text("🔄 Testando conexão com o Jackett...")

    healthy, response = await check_jackett_health()

    if healthy:
        await status_message.edit_text(
            "🟢 Jackett está acessível.\n\n"
            f"Endpoint: {JACKETT_HEALTH_URL}\n"
            f"Resposta: {response or '(vazia)'}"
        )

    else:
        await status_message.edit_text(
            "🔴 Não foi possível acessar o Jackett.\n\n"
            f"Endpoint: {JACKETT_HEALTH_URL}\n"
            f"Erro: {response}"
        )


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------


async def search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not authorized(update):
        return

    message = update.effective_message

    if message is None:
        return

    if not context.args:
        await message.reply_text("Informe o termo da busca após /search.")
        return

    search_query = " ".join(context.args).strip()

    await execute_search(
        update,
        context,
        search_query,
    )


# ---------------------------------------------------------------------------
# Fallback: qualquer mensagem de texto que não seja um comando também
# dispara uma busca, sem precisar digitar /search ou /s antes.
# ---------------------------------------------------------------------------


async def text_search_fallback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not authorized(update):
        return

    message = update.effective_message

    if message is None or not message.text:
        return

    search_query = message.text.strip()

    await execute_search(
        update,
        context,
        search_query,
    )


async def execute_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    search_query: str,
) -> None:
    """
    Lógica de busca compartilhada entre /search, /s, /b e o fallback
    de texto livre (text_search_fallback).
    """

    message = update.effective_message

    if message is None:
        return

    if not search_query:
        await message.reply_text("O termo de busca não pode estar vazio.")
        return

    if len(search_query) > 200:
        await message.reply_text("O termo de busca deve ter no máximo 200 caracteres.")
        return

    user = update.effective_user

    logger.info(
        "Busca recebida: user_id=%s query=%r",
        user.id if user else "?",
        search_query,
    )

    status_message = await message.reply_text(
        f"🔎 Buscando por: <b>{html.escape(search_query)}</b>...",
        parse_mode="HTML",
    )

    try:
        results = await search_jackett(
            search_query,
        )

    except httpx.TimeoutException:
        logger.exception("Timeout consultando Jackett")

        await status_message.edit_text("⏱️ O Jackett demorou demais para responder.")
        return

    except httpx.ConnectError:
        logger.exception("Falha de conexão com Jackett")

        await status_message.edit_text("🔴 Não foi possível conectar ao Jackett.")
        return

    except httpx.HTTPStatusError as exc:
        logger.exception(
            "Jackett respondeu HTTP %s",
            exc.response.status_code,
        )

        await status_message.edit_text(
            f"❌ O Jackett retornou HTTP {exc.response.status_code}."
        )
        return

    except ET.ParseError:
        logger.exception("Resposta XML inválida do Jackett")

        await status_message.edit_text(
            "❌ O Jackett retornou uma resposta XML inválida."
        )
        return

    except Exception:
        logger.exception("Erro inesperado durante a busca")

        await status_message.edit_text("❌ Ocorreu um erro inesperado durante a busca.")
        return

    if not results:
        await status_message.edit_text(
            f"🔎 Nenhum resultado para <b>{html.escape(search_query)}</b>.",
            parse_mode="HTML",
        )

        return

    # Guardamos a lista COMPLETA (até SEARCH_RESULT_LIMIT itens).
    # A navegação entre páginas depois é só uma fatia dessa lista,
    # sem nova consulta ao Jackett.
    context.user_data["search_query"] = search_query
    context.user_data["search_page"] = 0
    context.user_data["search_results"] = results

    page_results = results[0:RESULTS_PER_PAGE]

    has_next = len(results) > RESULTS_PER_PAGE

    text = build_results_message(
        search_query,
        page_results,
        page=0,
        total=len(results),
    )

    result_keyboard = build_result_keyboard(
        page_results,
        start_index=0,
    )

    pagination_keyboard = build_pagination_keyboard(
        page=0,
        has_next=has_next,
    )

    keyboard = combine_keyboards(
        result_keyboard,
        pagination_keyboard,
    )

    await status_message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Callback - Magnet
# ---------------------------------------------------------------------------


async def magnet_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    callback_query = update.callback_query

    if callback_query is None:
        return

    user = callback_query.from_user

    if not authorized_user_id(user.id):
        await callback_query.answer(
            "Você não está autorizado a usar este bot.",
            show_alert=True,
        )

        return

    data = callback_query.data or ""

    parts = data.split(":")

    if len(parts) != 2:
        return

    try:
        index = int(parts[1])
    except ValueError:
        await callback_query.answer(
            "Resultado inválido.",
            show_alert=True,
        )

        return

    results = context.user_data.get(
        "search_results",
        [],
    )

    if not isinstance(results, list) or index < 0 or index >= len(results):
        await callback_query.answer(
            "Resultado não encontrado.",
            show_alert=True,
        )

        return

    magnet = results[index].get("magnet")

    if not magnet:
        await callback_query.answer(
            "Este resultado não possui magnet.",
            show_alert=True,
        )

        return

    await callback_query.answer("Magnet enviado.")

    message = callback_query.message

    if message is not None:
        await message.reply_text(
            magnet,
            disable_web_page_preview=True,
        )


# ---------------------------------------------------------------------------
# Callback - Torrent
# ---------------------------------------------------------------------------


async def torrent_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    callback_query = update.callback_query

    if callback_query is None:
        return

    user = callback_query.from_user

    if not authorized_user_id(user.id):
        await callback_query.answer(
            "Você não está autorizado a usar este bot.",
            show_alert=True,
        )

        return

    data = callback_query.data or ""

    parts = data.split(":")

    if len(parts) != 2:
        return

    try:
        index = int(parts[1])
    except ValueError:
        await callback_query.answer(
            "Resultado inválido.",
            show_alert=True,
        )

        return

    results = context.user_data.get(
        "search_results",
        [],
    )

    if not isinstance(results, list) or index < 0 or index >= len(results):
        await callback_query.answer(
            "Resultado não encontrado.",
            show_alert=True,
        )

        return

    result = results[index]

    download_url = result.get("download_url")

    if not download_url:
        await callback_query.answer(
            "Este resultado não possui arquivo para download.",
            show_alert=True,
        )

        return

    if not download_url.startswith(("http://", "https://")):
        await callback_query.answer(
            "URL de download inválida.",
            show_alert=True,
        )

        return

    await callback_query.answer("Obtendo arquivo...")

    message = callback_query.message

    if message is None:
        return

    status_message = await message.reply_text("📥 Obtendo o arquivo...")

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".torrent",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name

        file_size, redirected_magnet = await download_torrent(
            download_url,
            temp_path,
        )

        # ---------------------------------------------------------------
        # O indexador redirecionou diretamente para magnet.
        # ---------------------------------------------------------------

        if redirected_magnet:
            await message.reply_text(
                redirected_magnet,
                disable_web_page_preview=True,
            )

            await status_message.delete()

            return

        # ---------------------------------------------------------------
        # Arquivo recebido.
        # ---------------------------------------------------------------

        if file_size <= 0:
            raise ValueError("O arquivo recebido está vazio.")

        filename = sanitize_filename(
            result.get(
                "title",
                "download",
            )
        )

        if not filename.lower().endswith(".torrent"):
            filename += ".torrent"

        logger.info(
            "Torrent obtido: user_id=%s size=%d title=%r",
            user.id,
            file_size,
            result.get("title"),
        )

        with open(
            temp_path,
            "rb",
        ) as torrent_file:
            await message.reply_document(
                document=InputFile(
                    torrent_file,
                    filename=filename,
                ),
                caption=(f"📥 {html.escape(result['title'])}"),
                parse_mode="HTML",
            )

        await status_message.delete()

    except httpx.TimeoutException:
        logger.exception("Timeout baixando torrent")

        await status_message.edit_text("⏱️ O download demorou demais.")

    except httpx.ConnectError:
        logger.exception("Erro de conexão baixando torrent")

        await status_message.edit_text(
            "🔴 Não foi possível conectar ao Jackett para obter o arquivo."
        )

    except httpx.HTTPStatusError as exc:
        logger.exception(
            "HTTP %s baixando torrent",
            exc.response.status_code,
        )

        await status_message.edit_text(
            f"❌ O servidor retornou HTTP {exc.response.status_code}."
        )

    except ValueError as exc:
        logger.exception("Arquivo torrent inválido")

        await status_message.edit_text(f"❌ {exc}")

    except Exception:
        logger.exception("Erro inesperado ao baixar torrent")

        await status_message.edit_text("❌ Não foi possível obter o arquivo torrent.")

    finally:
        if temp_path:
            try:
                os.unlink(temp_path)

            except FileNotFoundError:
                pass

            except Exception:
                logger.exception("Não foi possível remover arquivo temporário")


# ---------------------------------------------------------------------------
# Callback - Paginação
# ---------------------------------------------------------------------------


async def page_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    callback_query = update.callback_query

    if callback_query is None:
        return

    user = callback_query.from_user

    if not authorized_user_id(user.id):
        await callback_query.answer(
            "Você não está autorizado a usar este bot.",
            show_alert=True,
        )

        return

    data = callback_query.data or ""

    parts = data.split(":")

    if len(parts) != 2:
        return

    try:
        page = int(parts[1])
    except ValueError:
        await callback_query.answer(
            "Página inválida.",
            show_alert=True,
        )

        return

    if page < 0:
        await callback_query.answer(
            "Página inválida.",
            show_alert=True,
        )

        return

    search_query = context.user_data.get("search_query")

    all_results = context.user_data.get("search_results")

    if not search_query or not isinstance(all_results, list):
        await callback_query.answer(
            "Faça uma nova busca.",
            show_alert=True,
        )

        return

    # Paginação 100% local: nenhuma nova consulta ao Jackett é feita
    # aqui. Apenas fatiamos a lista completa que já buscamos em
    # /search. Isso corrige a paginação (o "offset" do Torznab não é
    # confiável no indexador "all") e evita sobrecarregar o Jackett
    # a cada clique.

    start = page * RESULTS_PER_PAGE
    page_results = all_results[start : start + RESULTS_PER_PAGE]

    if not page_results:
        await callback_query.answer(
            "Não existem mais resultados.",
            show_alert=True,
        )

        return

    context.user_data["search_page"] = page

    has_next = len(all_results) > start + RESULTS_PER_PAGE

    text = build_results_message(
        search_query,
        page_results,
        page,
        total=len(all_results),
    )

    result_keyboard = build_result_keyboard(
        page_results,
        start_index=start,
    )

    pagination_keyboard = build_pagination_keyboard(
        page,
        has_next,
    )

    keyboard = combine_keyboards(
        result_keyboard,
        pagination_keyboard,
    )

    if callback_query.message is None:
        return

    await callback_query.answer()

    await callback_query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Erro global
# ---------------------------------------------------------------------------


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    logger.error(
        "Erro não tratado no Telegram Bot",
        exc_info=context.error,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:

    logger.info("Iniciando Telegram Bot...")

    logger.info(
        "Jackett: %s",
        JACKETT_URL,
    )

    logger.info(
        "Usuários autorizados: %d",
        len(TELEGRAM_ALLOWED_USER_IDS),
    )

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ping",
            ping,
        )
    )

    application.add_handler(
        CommandHandler(
            "search",
            search,
        )
    )

    application.add_handler(
        CommandHandler(
            "s",
            search,
        )
    )

    application.add_handler(
        CommandHandler(
            "b",
            search,
        )
    )

    # Fallback: texto que não é comando também dispara busca.
    # Registrado depois dos CommandHandlers para não competir com eles;
    # o filtro ~filters.COMMAND já garante que "/algo" nunca cai aqui.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_search_fallback,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            magnet_callback,
            pattern=r"^magnet:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            torrent_callback,
            pattern=r"^torrent:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            page_callback,
            pattern=r"^page:\d+$",
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot iniciado. Aguardando mensagens...")

    application.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
        ]
    )


if __name__ == "__main__":
    main()
