import logging
import os
import sys
import time
from typing import Any, Dict, List

import requests
import telebot
from dotenv import load_dotenv

load_dotenv()

PRACTICUM_TOKEN = os.getenv("PRACTICUM_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RETRY_PERIOD = 600
ENDPOINT = "https://practicum.yandex.ru/api/user_api/homework_statuses/"
HEADERS = {"Authorization": f"OAuth {PRACTICUM_TOKEN}"}


HOMEWORK_VERDICTS = {
    "approved": "Работа проверена: ревьюеру всё понравилось. Ура!",
    "reviewing": "Работа взята на проверку ревьюером.",
    "rejected": "Работа проверена: у ревьюера есть замечания.",
}


# Собственные исключения
class BotError(Exception):
    """Базовое исключение бота."""


class TokenError(BotError):
    """Отсутствуют обязательные переменные окружения."""


class APIError(BotError):
    """Ошибка при обращении к API Практикума."""


class ResponseError(BotError):
    """Неправильный формат ответа API."""


class StatusError(BotError):
    """Неожиданный статус домашней работы."""


# Настройка логирования
logger = logging.getLogger("homework_logger")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(stream=sys.stdout)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)


def check_tokens() -> bool:
    """Проверяет, что все необходимые переменные окружения доступны.

    Возвращает True, если все токены присутствуют, иначе False.
    При отсутствии хотя бы одной переменной логирует CRITICAL.
    """
    tokens = {
        "PRACTICUM_TOKEN": PRACTICUM_TOKEN,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }
    missing = [name for name, val in tokens.items() if not val]
    if missing:
        logger.critical(
            "Отсутствует обязательная переменная окружения: %s. "
            "Программа принудительно остановлена.",
            ", ".join(f"'{m}'" for m in missing),
        )
        return False
    return True


def send_message(bot: telebot.TeleBot, message: str) -> bool:
    """Отправляет сообщение в Telegram и логирует результат.

    Возвращает True при успешной отправке, False при ошибке.
    """
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.debug('Бот отправил сообщение "%s"', message)
        return True
    except Exception as error:
        logger.error("Сбой при отправке сообщения в Telegram: %s", error)
        return False


def get_api_answer(timestamp: int) -> Dict[str, Any]:
    """Делает запрос к API Практикума и возвращает ответ в виде dict.

    Аргументы:
        timestamp: временная метка (seconds) - параметр from_date для API.

    В случае проблем с сетевым запросом или кода ответа отличного от 200
    возбуждает APIError.
    """
    params = {"from_date": timestamp}
    try:
        response = requests.get(
            ENDPOINT, headers=HEADERS, params=params, timeout=10
        )
    except requests.RequestException as error:
        logger.error("Сбой при запросе к эндпоинту %s: %s", ENDPOINT, error)
        raise APIError(f"Ошибка запроса к API: {error}")

    if response.status_code != 200:
        logger.error(
            "Эндпоинт %s недоступен. Код ответа API: %s",
            ENDPOINT,
            response.status_code,
        )
        raise APIError(
            f"Эндпоинт {ENDPOINT} вернул статус {response.status_code}"
        )

    try:
        return response.json()
    except ValueError as error:
        logger.error("Не удалось распарсить JSON из ответа API: %s", error)
        raise APIError("Невозможно декодировать JSON в ответе API")


def check_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Проверяет структуру ответа API и возвращает список домашних работ.

    Ожидается, что response - это словарь,
    содержащий ключ 'homeworks' со списком.
    В противном случае возбуждается ResponseError.
    """
    if not isinstance(response, dict):
        logger.error("Ответ API имеет неверный тип: %s", type(response))
        raise TypeError("Ожидался dict в ответе API")

    if "homeworks" not in response:
        logger.error("Отсутствуют ожидаемые ключи в ответе API: 'homeworks'")
        raise ResponseError("В ответе API отсутствует ключ 'homeworks'")

    homeworks = response.get("homeworks")
    if not isinstance(homeworks, list):
        logger.error(
            "Ключ 'homeworks' не содержит список - тип: %s", type(homeworks)
        )
        raise TypeError("Ключ 'homeworks' должен содержать список")

    return homeworks


def parse_status(homework: Dict[str, Any]) -> str:
    """Извлекает статус домашней работы и формирует сообщение для Telegram.

    Ожидается, что homework содержит ключи 'homework_name' и 'status'.
    В случае неизвестного статуса возбуждается StatusError.
    """
    if not isinstance(homework, dict):
        logger.error(
            "Элемент homeworks имеет неверный тип: %s", type(homework)
        )
        raise ResponseError("Элемент homeworks должен быть словарём")

    homework_name = homework.get("homework_name")
    if homework_name is None:
        logger.error("В ответе отсутствует поле 'homework_name'")
        raise ResponseError(
            "В информации о домашней работе отсутствует 'homework_name'"
        )

    status = homework.get("status")
    if status is None:
        logger.error(
            "В ответе отсутствует поле 'status' для работы %s", homework_name
        )
        raise ResponseError(
            "В информации о домашней работе отсутствует 'status'"
        )

    verdict = HOMEWORK_VERDICTS.get(status)
    if verdict is None:
        logger.error("Неожиданный статус домашней работы: %s", status)
        raise StatusError(f"Неизвестный статус домашней работы: {status}")

    return f'Изменился статус проверки работы "{homework_name}". {verdict}'


def main() -> None:
    """Основная логика работы бота.

    Последовательно: проверяем токены, создаём объект бота, опрашиваем API,
    проверяем ответ и при наличии изменений отправляем уведомление.
    """
    if not check_tokens():
        # Нельзя продолжать работу без токенов
        raise SystemExit("Отсутствуют необходимые переменные окружения.")

    bot = telebot.TeleBot(TELEGRAM_TOKEN)
    timestamp = int(time.time())

    last_error_message = ""
    last_homework_state = None

    while True:
        try:
            response = get_api_answer(timestamp)
            homeworks = check_response(response)

            if not homeworks:
                logger.debug("Отсутствие в ответе новых статусов")
            else:
                # Берём первый элемент - самый свежий
                homework = homeworks[0]
                message = parse_status(homework)
                # Проверяем, изменился ли статус
                # относительно последнего отправленного
                if message != last_homework_state:
                    send_message(bot, message)
                    last_homework_state = message
                else:
                    logger.debug("Статус домашней работы не изменился")

            # Обновляем timestamp на текущее значение в ответе, если есть
            current_date = response.get("current_date")
            if isinstance(current_date, (int, float)):
                timestamp = int(current_date)
            else:
                # если current_date отсутствует, просто ставим текущее время
                timestamp = int(time.time())

            # Сброс сообщений об ошибках после успешного шага
            last_error_message = ""

        except Exception as error:
            message = f"Сбой в работе программы: {error}"
            logger.exception(message)
            # Если ошибка новая - пробуем отправить в телеграм (один раз)
            if str(error) != last_error_message:
                if send_message(bot, message):
                    last_error_message = str(error)
        finally:
            time.sleep(RETRY_PERIOD)


if __name__ == "__main__":
    main()
