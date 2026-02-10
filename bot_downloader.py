import os
import asyncio
import aiohttp
import time
import logging
import re
from pyrogram import Client, filters, enums, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from datetime import datetime, timedelta
from urllib.parse import unquote
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot_downloader.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================================================================================
# CONFIGURATION
# ==================================================================================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DOWNLOAD_DIR = "downloads"
PARA_ENVIAR_DIR = "para_enviar" 
ARCHIVE_CHAT_ID = int(os.getenv("ARCHIVE_CHAT_ID", "0"))
ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", "").split(",")] if os.getenv("ALLOWED_USERS") else []

# Headers para simular um navegador real (Evita bloqueio do servidor)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity;q=1, *;q=0",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

for folder in [DOWNLOAD_DIR, PARA_ENVIAR_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Sistema de cancelamento e estado
active_downloads = {}  # {message_id: {"cancel": False, "file_path": "..."}}

def format_bytes(size):
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

async def progress_callback(current, total, message, action, start_time, cancel_btn=None):
    # Verifica cancelamento
    if message.id in active_downloads and active_downloads[message.id].get("cancel"):
        raise Exception("Upload cancelado pelo usuário")
        
    now = time.time()
    if not hasattr(progress_callback, "last_update"):
        progress_callback.last_update = 0
    if now - progress_callback.last_update < 2.0: return  # Atualiza a cada 2s para economizar API
    progress_callback.last_update = now
    
    elapsed = now - start_time
    if total > 0:
        percentage = (current / total) * 100
    else:
        percentage = 0
        
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    
    progress_bar = "".join(["▰" if i < percentage // 5 else "▱" for i in range(20)])
    
    # Formata tempos
    elapsed_str = str(timedelta(seconds=int(elapsed)))
    eta_str = str(timedelta(seconds=int(eta)))
    
    text = (
        f"<b>{action}...</b>\n\n"
        f"<code>{progress_bar}</code> {percentage:.1f}%\n\n"
        f"📦 <b>Tamanho:</b> {format_bytes(current)} / {format_bytes(total)}\n"
        f"⚡ <b>Velocidade:</b> {format_bytes(speed)}/s\n"
        f"⏱ <b>Decorrido:</b> {elapsed_str}\n"
        f"⏳ <b>Restante:</b> {eta_str}"
    )
    try: 
        await message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=cancel_btn)
    except: pass

async def download_worker(session, url, start, end, file_path, semaphore, progress_dict, thread_idx):
    headers = HEADERS.copy()
    headers["Range"] = f"bytes={start}-{end}"
    
    async with semaphore:
        for attempt in range(5):
            try:
                async with session.get(url, headers=headers, timeout=60) as resp:
                    if resp.status not in [200, 206]:
                        raise Exception(f"HTTP {resp.status}")
                    
                    with open(file_path, "r+b") as f:
                        f.seek(start)
                        async for chunk in resp.content.iter_chunked(1024 * 1024): # 1MB chunks
                            if chunk:
                                f.write(chunk)
                                progress_dict[thread_idx] += len(chunk)
                    return # Sucesso
            except (aiohttp.ClientPayloadError, aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
                logger.warning(f"Thread {thread_idx} tentativa {attempt+1}/5 falhou (Rede): {e}")
                progress_dict[thread_idx] = 0 # Reseta progresso visual desse chunk
                await asyncio.sleep(2 * (attempt + 1)) # Backoff exponencial
            except Exception as e:
                logger.error(f"Thread {thread_idx} erro crítico: {e}")
                progress_dict[thread_idx] = 0
                await asyncio.sleep(2)
        
        raise Exception(f"Falha na Thread {thread_idx} após 5 tentativas")

async def download_handler(client, message: Message, custom_name=None, url=None):
    """
    Handler unificado para downloads.
    """
    if message.from_user and message.from_user.id not in ALLOWED_USERS:
        await message.reply_text("❌ Você não tem permissão para usar este bot.")
        return
    
    if not url:
        text = message.text.strip()
        logger.info(f"Download solicitado por {message.from_user.id if message.from_user else 'Unknown'}")
        
        nome_match = re.search(r'Nome:\s*(.+)', text)
        link_match = re.search(r'Link:\s*(.+)', text)
        
        if nome_match and link_match:
            custom_name = nome_match.group(1).strip()
            url = link_match.group(1).strip()
        elif text.startswith("http"):
            url = text
        else:
            return  # Não é um link válido
    
    msg = await message.reply_text("🔎 <b>Analisando Link...</b>", parse_mode=enums.ParseMode.HTML)
    start_time = time.time()
    file_path = None
    
    # Detecta expiração do link
    expiration_info = ""
    expired_match = re.search(r'expired=(\d+)', url)
    if expired_match:
        expire_timestamp = int(expired_match.group(1))
        now_timestamp = int(time.time())
        time_remaining = expire_timestamp - now_timestamp
        
        if time_remaining < 0:
            await msg.edit_text(
                f"❌ <b>Link Expirado!</b>\n\n"
                f"⏰ Expirou há {abs(time_remaining // 60)} minutos.\n"
                f"🔄 Extraia um novo link com o extrator.py",
                parse_mode=enums.ParseMode.HTML
            )
            return
        else:
            expire_date = datetime.fromtimestamp(expire_timestamp)
            if time_remaining < 3600:
                minutes_left = time_remaining // 60
                expiration_info = f"\n⚠️ <b>Expira em {minutes_left} min</b> ({expire_date.strftime('%H:%M')})"
            elif time_remaining < 86400:
                hours_left = time_remaining // 3600
                expiration_info = f"\n⏰ <b>Expira em {hours_left}h</b> ({expire_date.strftime('%H:%M')})"
            else:
                days_left = time_remaining // 86400
                expiration_info = f"\n📅 <b>Expira em {days_left} dias</b> ({expire_date.strftime('%d/%m %H:%M')})"
    
    try:
        connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
        async with aiohttp.ClientSession(headers=HEADERS, connector=connector, trust_env=True) as session:
            async with session.head(url, timeout=30) as r:
                total_size = int(r.headers.get('content-length', 0))
                
                if not custom_name:
                    filename = f"video_{int(time.time())}.mp4"
                    cd = r.headers.get("Content-Disposition")
                    if cd and "filename=" in cd:
                        try: filename = cd.split("filename=")[1].strip(" \"'")
                        except: pass
                    else:
                        try:
                            possible_name = unquote(url.split("/")[-1].split("?")[0])
                            if possible_name.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".ts")):
                                filename = possible_name
                        except: pass
                else:
                    filename = custom_name if custom_name.endswith(".mp4") else f"{custom_name}.mp4"
            
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            
            cancel_btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancelar Download", callback_data=f"cancel_{msg.id}")
            ]])
            active_downloads[msg.id] = {"cancel": False, "file_path": file_path}
            
            if total_size == 0:
                await msg.edit_text(
                    f"⏳ <b>Baixando (Fluxo Contínuo):</b>\n<code>{filename}</code>{expiration_info}\n\n⚠️ Tamanho desconhecido",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=cancel_btn
                )
                async with session.get(url, timeout=120) as resp:
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            if active_downloads.get(msg.id, {}).get("cancel"):
                                raise Exception("Download cancelado pelo usuário")
                            if chunk:
                                f.write(chunk)
                total_size = os.path.getsize(file_path)
            else:
                try:
                    await msg.edit_text(
                        f"⏳ <b>Baixando (Turbo Quad-Engine):</b>\n<code>{filename}</code>{expiration_info}",
                        parse_mode=enums.ParseMode.HTML,
                        reply_markup=cancel_btn
                    )
                    
                    num_parts = 8 
                    semaphore_limit = 4 
                    
                    chunk_size = total_size // num_parts
                    progress_dict = [0] * num_parts
                    semaphore = asyncio.Semaphore(semaphore_limit)
                    
                    with open(file_path, "wb") as f: f.truncate(total_size)
                    
                    tasks = []
                    for i in range(num_parts):
                        start = i * chunk_size
                        end = (i + 1) * chunk_size - 1 if i < num_parts - 1 else total_size - 1
                        task = asyncio.create_task(download_worker(
                            session, url,
                            start, end,
                            file_path, semaphore, progress_dict, i
                        ))
                        tasks.append(task)
                        await asyncio.sleep(0.2) # Ramp-up suave
                    
                    monitor = True
                    async def monitor_speed():
                        while monitor:
                            if active_downloads.get(msg.id, {}).get("cancel"):
                                return
                            await progress_callback(sum(progress_dict), total_size, msg, "Baixando", start_time, cancel_btn)
                            await asyncio.sleep(1.5)
                    
                    m_task = asyncio.create_task(monitor_speed())
                    
                    try:
                        pending = set(tasks)
                        while pending:
                            done, pending = await asyncio.wait(pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                            if active_downloads.get(msg.id, {}).get("cancel"):
                                for task in pending: task.cancel()
                                monitor = False
                                raise Exception("Download cancelado pelo usuário")
                            for task in done:
                                if task.exception():
                                    monitor = False
                                    raise task.exception()

                    except asyncio.CancelledError:
                        raise Exception("Download cancelado")
                    
                    monitor = False
                    await m_task
                    
                except Exception as parallel_error:
                    monitor = False
                    for task in tasks: task.cancel() # Cancela pendentes
                    await asyncio.wait(tasks, timeout=2) # Espera limpeza
                    
                    logger.warning(f"Download turbo falhou: {parallel_error}. Tentando modo seguro...")
                    await msg.edit_text(
                        f"⏳ <b>Baixando (Modo Seguro):</b>\n<code>{filename}</code>{expiration_info}\n\n⚠️ Servidor instável, usando 1 conexão.",
                        parse_mode=enums.ParseMode.HTML,
                        reply_markup=cancel_btn
                    )
                    
                    downloaded = 0
                    with open(file_path, "wb") as f:
                        async with session.get(url, timeout=300) as resp:
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                if active_downloads.get(msg.id, {}).get("cancel"):
                                    raise Exception("Cancelado")
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if downloaded % (5 * 1024 * 1024) == 0: 
                                        await progress_callback(downloaded, total_size, msg, "Baixando (Seguro)", start_time, cancel_btn)


        if total_size > 0 and os.path.exists(file_path) and os.path.getsize(file_path) == 0:
             raise Exception("Erro crítico: Arquivo vazio baixado.")

        if os.path.getsize(file_path) < total_size:
            raise Exception("Tamanho do arquivo final inconsistente")
        
        await msg.edit_text("📤 <b>Download 100%! Enviando...</b>", parse_mode=enums.ParseMode.HTML, reply_markup=cancel_btn)
        caption = f"🎬 <b>Arquivo:</b> <code>{filename}</code>\n📦 <b>Tamanho:</b> {format_bytes(total_size)}"
        
        if message.chat.id == ARCHIVE_CHAT_ID:
            chat_destino = message.chat.id
        else:
            chat_destino = ARCHIVE_CHAT_ID

        await client.send_video(
            chat_id=chat_destino,
            video=file_path,
            caption=f"{caption}\n👤 <b>Pedido por:</b> {message.from_user.mention if message.from_user else 'User'}",
            supports_streaming=True,
            progress=progress_callback,
            progress_args=(msg, "Enviando para Arquivo", time.time(), cancel_btn)
        )
        
        if message.chat.id != ARCHIVE_CHAT_ID:
            await msg.edit_text(f"✅ <b>Upload Concluído!</b>\nO arquivo foi enviado para o canal de arquivo.\n\n� <b>Arquivo:</b> <code>{filename}</code>", parse_mode=enums.ParseMode.HTML)
        else:
            await msg.delete()
        
        await msg.delete()
        
    except Exception as e:
        logger.error(f"Erro no download: {e}", exc_info=True)
        await message.reply_text(f"❌ <b>Erro:</b> {str(e)}", parse_mode=enums.ParseMode.HTML)
    finally:
        if msg.id in active_downloads:
            del active_downloads[msg.id]
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

async def importar_csv_handler(client, message: Message):
    import csv
    if message.from_user and message.from_user.id not in ALLOWED_USERS:
        return
    
    if not message.document:
        await message.reply_text("❌ Envie o arquivo CSV ou TXT!")
        return
    
    file_ext = message.document.file_name.split('.')[-1].lower()
    if file_ext not in ['csv', 'txt']:
        await message.reply_text("❌ O arquivo deve ser CSV ou TXT!")
        return
    
    status_msg = await message.reply_text("📥 <b>Baixando arquivo...</b>", parse_mode=enums.ParseMode.HTML)
    file_path = await message.download(file_name=f"temp_{int(time.time())}.{file_ext}")
    
    filmes = []
    try:
        if file_ext == 'csv':
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    if "nome" in row and "link" in row and row["nome"] and row["link"]:
                        filmes.append({"nome": row["nome"].strip(), "link": row["link"].strip()})
        else:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                blocos = content.split('-' * 50)
                for bloco in blocos:
                    nome_match = re.search(r'Nome:\s*(.+)', bloco)
                    link_match = re.search(r'Link:\s*(.+)', bloco)
                    if nome_match and link_match:
                        nome = nome_match.group(1).strip()
                        link = link_match.group(1).strip()
                        if nome and link and not link.startswith("ERRO"):
                            filmes.append({"nome": nome, "link": link})
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Erro ao ler arquivo:</b> {str(e)}", parse_mode=enums.ParseMode.HTML)
        return
    finally:
        if os.path.exists(file_path): os.remove(file_path)
    
    if not filmes:
        await status_msg.edit_text("❌ Arquivo vazio ou formato inválido!")
        return
    
    await status_msg.edit_text(f"✅ <b>{len(filmes)} filmes encontrados!</b>\n🚀 Iniciando downloads...", parse_mode=enums.ParseMode.HTML)
    
    sucesso = 0
    falhas = 0
    
    for i, filme in enumerate(filmes, 1):
        try:
            progress_msg = await message.reply_text(
                f"<b>[{i}/{len(filmes)}]</b> {filme['nome'][:50]}...",
                parse_mode=enums.ParseMode.HTML
            )
            
            await download_handler(client, progress_msg, custom_name=filme["nome"], url=filme["link"])
            sucesso += 1
            
        except Exception as e:
            falhas += 1
            await message.reply_text(f"❌ <b>Erro em '{filme['nome'][:20]}':</b> {str(e)}", parse_mode=enums.ParseMode.HTML)
        
        await asyncio.sleep(2)
    
    await message.reply_text(
        f"🎉 <b>Concluído!</b>\n✅ Sucesso: {sucesso}\n❌ Falhas: {falhas}",
        parse_mode=enums.ParseMode.HTML
    )

# ==================================================================================
# SCAN E AUTO-SENDER
# ==================================================================================
async def scan_handler(client, message: Message):
    """Verifica arquivos orfãos na pasta de downloads"""
    if message.from_user.id not in ALLOWED_USERS: return

    # Filtra arquivos, ignorando os que estão sendo baixados agora
    active_files = [v["file_path"] for v in active_downloads.values()]
    found_files = []
    
    if os.path.exists(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            f_path = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(f_path) and f_path not in active_files:
                if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.ts')):
                    if not f.startswith("temp_"): # Ignora temps de importação
                        found_files.append(f_path)
    
    if not found_files:
        await message.reply_text("✅ <b>Nenhum arquivo pendente encontrado!</b>", parse_mode=enums.ParseMode.HTML)
        return

    text = f"📂 <b>{len(found_files)} Arquivos Encontrados:</b>\n\n"
    text += "\n".join([f"• <code>{os.path.basename(f)}</code>" for f in found_files[:10]])
    if len(found_files) > 10: text += f"\n... e mais {len(found_files)-10}"
    
    text += "\n\n🤔 <b>Você já fez upload destes arquivos?</b>"
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sim (Excluir Tudo)", callback_data="scan_yes")],
        [InlineKeyboardButton("❌ Não (Fazer Upload)", callback_data="scan_no")]
    ])
    
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb)

async def scan_callback_handler(client, callback_query):
    if callback_query.data not in ["scan_yes", "scan_no"]: return
    
    # Re-verifica arquivos
    active_files = [v["file_path"] for v in active_downloads.values()]
    found_files = []
    if os.path.exists(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            f_path = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(f_path) and f_path not in active_files:
                if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.ts')):
                     if not os.path.basename(f).startswith("temp_"):
                        found_files.append(f_path)

    if not found_files:
        await callback_query.message.edit_text("❌ Nenhum arquivo encontrado agora.")
        return

    if callback_query.data == "scan_yes":
        count = 0
        for f_path in found_files:
            try: os.remove(f_path); count += 1
            except: pass
        await callback_query.message.edit_text(f"🗑 <b>{count} arquivos excluídos!</b>", parse_mode=enums.ParseMode.HTML)

    elif callback_query.data == "scan_no":
        status_msg = await callback_query.message.edit_text(f"🚀 <b>Iniciando Upload de {len(found_files)} arquivos...</b>", parse_mode=enums.ParseMode.HTML)
        
        # Registra cancelamento
        active_downloads[status_msg.id] = {"cancel": False, "file_path": "BATCH_UPLOAD"}
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancelar Tudo", callback_data=f"cancel_{status_msg.id}")]])
        
        success = 0
        canceled = False
        
        for i, f_path in enumerate(found_files, 1):
            if active_downloads.get(status_msg.id, {}).get("cancel"):
                canceled = True
                break
                
            filename = os.path.basename(f_path)
            try:
                await status_msg.edit_text(
                    f"📤 <b>Enviando [{i}/{len(found_files)}]:</b>\n<code>{filename}</code>", 
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=cancel_btn
                )
                
                caption = f"🎬 <b>Arquivo Recuperado:</b> <code>{filename}</code>\n📦 <b>Tamanho:</b> {format_bytes(os.path.getsize(f_path))}"
                
                await client.send_video(
                    chat_id=ARCHIVE_CHAT_ID,
                    video=f_path,
                    caption=caption,
                    supports_streaming=True,
                    progress=progress_callback,
                    progress_args=(status_msg, f"Enviando [{i}/{len(found_files)}]", time.time(), cancel_btn)
                )
                os.remove(f_path)
                success += 1
            except Exception as e:
                # Se for erro de cancelamento, para o loop
                if "cancelado pelo usuário" in str(e):
                    canceled = True
                    break
                logger.error(f"Erro no upload scan: {e}")
                try: await client.send_message(callback_query.message.chat.id, f"❌ Erro ao enviar <code>{filename}</code>: {str(e)}", parse_mode=enums.ParseMode.HTML)
                except: pass
            
            await asyncio.sleep(2)
        
        # Cleanup
        if status_msg.id in active_downloads: del active_downloads[status_msg.id]
        
        if canceled:
            await status_msg.edit_text(f"🛑 <b>Operação Cancelada!</b>\nUploads com sucesso: {success}/{len(found_files)}", parse_mode=enums.ParseMode.HTML)
        else:
            await status_msg.edit_text(
                f"✅ <b>Scan Finalizado!</b>\nUploads: {success}/{len(found_files)}",
                parse_mode=enums.ParseMode.HTML
            )

async def auto_sender_worker(app):
    while True:
        try:
            files = [f for f in os.listdir(PARA_ENVIAR_DIR) if os.path.isfile(os.path.join(PARA_ENVIAR_DIR, f))]
            for file in files:
                file_path = os.path.join(PARA_ENVIAR_DIR, file)
                try:
                    s1 = os.path.getsize(file_path)
                    await asyncio.sleep(3)
                    if s1 != os.path.getsize(file_path): continue
                except: continue
                
                if os.path.splitext(file)[1].lower() in ['.mp4', '.mkv', '.avi', '.mov']:
                    try: await app.send_video(chat_id=ARCHIVE_CHAT_ID, video=file_path, supports_streaming=True)
                    except: pass
                    finally:
                        if os.path.exists(file_path): os.remove(file_path)
        except: pass
        await asyncio.sleep(10)

async def cancel_callback(client, callback_query):
    try:
        msg_id = int(callback_query.data.split("_")[1])
        if msg_id in active_downloads:
            active_downloads[msg_id]["cancel"] = True
            await callback_query.answer("⏹ Cancelando...")
        else:
            await callback_query.answer("Download já finalizado", show_alert=False)
    except: pass

if __name__ == "__main__":
    print(">> STARTING...")
    
    # Validação de libs para desempenho
    try:
        import tgcrypto
        print(">> TgCrypto: DETECTADO! (Aceleracao Ativada)")
    except ImportError:
        print(">> TgCrypto: NAO DETECTADO! (Velocidade Limitada)")
        print(">> Instale com: pip install tgcrypto")

    # OTIMIZAÇÃO DE UPLOAD (AJUSTE FINO):
    # - workers: 16 (Equilíbrio entre CPU e I/O)
    # - max_concurrent_transmissions: 6 (Sweet Spot para evitar engarrafamento TCP)
    # - ipv6: False (Evita erro 1231)
    app = Client(
        "bot_downloader", 
        api_id=API_ID, 
        api_hash=API_HASH, 
        bot_token=BOT_TOKEN,
        workers=16,
        max_concurrent_transmissions=6,
        ipv6=False
    )
    
    app.add_handler(MessageHandler(download_handler, filters.text & ~filters.command(["start", "scan", "help", "importar"])))
    app.add_handler(MessageHandler(scan_handler, filters.command("scan")))
    app.add_handler(MessageHandler(importar_csv_handler, filters.document))
    app.on_callback_query(filters.regex(r"^cancel_"))(cancel_callback)
    app.on_callback_query(filters.regex(r"^scan_"))(scan_callback_handler)
    
    print(">> BOT DUAL ENGINE ONLINE (TUNED v3)")
    print(f">> Workers: {app.workers} | Max Transmissions: {app.max_concurrent_transmissions}")
    
    app.run()
