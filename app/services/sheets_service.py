"""
Google Sheets: upsert de leads.
Colunas: Data/Hora | WhatsApp | Nome | Resumo da Conversa
"""
import json
import logging
from datetime import datetime

from app.config import settings

logger = logging.getLogger(__name__)

HEADERS = ["Data/Hora", "WhatsApp", "Nome", "Resumo da Conversa"]

_sheet = None


def _get_sheet():
    global _sheet
    if _sheet is not None:
        return _sheet

    if not settings.GOOGLE_CREDENTIALS_JSON or not settings.GOOGLE_SHEET_ID:
        logger.warning("Google Sheets nao configurado (GOOGLE_CREDENTIALS_JSON ou GOOGLE_SHEET_ID ausente)")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        creds_info = json.loads(settings.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(settings.GOOGLE_SHEET_ID)
        _sheet = spreadsheet.sheet1
        logger.info("Google Sheets conectado: %s", settings.GOOGLE_SHEET_ID)
    except Exception:
        logger.exception("Erro ao conectar Google Sheets")
        return None

    return _sheet


def upsert_lead(phone: str, name: str = "", resumo: str = "") -> None:
    """Insere ou atualiza linha do lead na planilha (busca por telefone)."""
    sheet = _get_sheet()
    if sheet is None:
        return

    try:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        all_values = sheet.get_all_values()

        # Garante cabecalho na linha 1
        if not all_values or all_values[0] != HEADERS:
            sheet.insert_row(HEADERS, 1)
            all_values = sheet.get_all_values()

        # Busca linha existente pelo telefone (coluna B)
        existing_row_index = None
        existing_row = []
        for i, row in enumerate(all_values[1:], start=2):  # pula cabecalho
            if len(row) > 1 and row[1] == phone:
                existing_row_index = i
                existing_row = row
                break

        if existing_row_index:
            updated_name = name if name else (existing_row[2] if len(existing_row) > 2 else "")
            updated_resumo = resumo if resumo else (existing_row[3] if len(existing_row) > 3 else "")
            sheet.update(
                f"A{existing_row_index}:D{existing_row_index}",
                [[now, phone, updated_name, updated_resumo]],
            )
            logger.info("Sheets: lead %s atualizado", phone)
        else:
            sheet.append_row([now, phone, name, resumo])
            logger.info("Sheets: lead %s inserido", phone)

    except Exception:
        logger.exception("Erro ao salvar lead %s no Sheets", phone)
