import time
import uuid
import logging
import httpx
from config import PANEL_URL, PANEL_USERNAME, PANEL_PASSWORD, INBOUND_ID, VPN_DOMAIN

class VPNManager:
    def __init__(self):
        self.session = httpx.AsyncClient(verify=False, timeout=10.0)
        self.cookies = None

    async def login(self) -> bool:
        """Авторизация в панели 3X-UI"""
        try:
            url = f"{PANEL_URL}/login"
            payload = {"username": PANEL_USERNAME, "password": PANEL_PASSWORD}
            response = await self.session.post(url, data=payload)
            if response.status_code == 200 and response.json().get("success"):
                self.cookies = response.cookies
                return True
            logging.error("Ошибка входа в 3X-UI: неверный логин или пароль")
            return False
        except Exception as e:
            logging.error(f"Не удалось подключиться к VPN панели: {e}")
            return False

    async def create_client_key(self, user_id: int, username: str, days: int = 30) -> str | None:
        """Создание VLESS клиента в 3X-UI"""
        if not await self.login():
            return None

        client_uuid = str(uuid.uuid4())
        expiry_time = int((time.time() + (days * 86400)) * 1000)

        client_data = {
            "id": client_uuid,
            "alterId": 0,
            "email": f"id{user_id}_{username or 'user'}",
            "limitIp": 2,
            "totalGB": 0,
            "expiryTime": expiry_time,
            "enable": True,
            "tgId": str(user_id),
            "subId": f"sub_{user_id}"
        }

        import json
        payload = {
            "id": INBOUND_ID,
            "settings": json.dumps({"clients": [client_data]})
        }

        url = f"{PANEL_URL}/panel/api/inbounds/addClient"
        try:
            response = await self.session.post(url, json=payload, cookies=self.cookies)
            res_json = response.json()

            if res_json.get("success"):
                # Конструируем стандартную VLESS ссылку
                vless_key = f"vless://{client_uuid}@{VPN_DOMAIN}:443?type=tcp&security=reality#{user_id}_VPN"
                return vless_key
            else:
                logging.error(f"Панель вернула ошибку при добавлении: {res_json}")
                return None
        except Exception as e:
            logging.error(f"Ошибка HTTP-запроса к API 3X-UI: {e}")
            return None

vpn_manager = VPNManager()