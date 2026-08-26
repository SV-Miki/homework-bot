import logging
import os
import sys
import time
from http import HTTPStatus
from typing import Any

import requests
import telebot
from dotenv import load_dotenv

from exceptions import (
    APIError,
    ResponseError,
    StatusError,
    TokenError,
)

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

logger = logging.getLogger("homework_logger")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def check_tokens() -> None:
    """Проверяет наличие обязательных переменных окружения.

    При отсутствии хотя бы одной переменной возбуждает TokenError.
    """
    missing = [
        name
        for name, value in {
            "PRACTICUM_TOKEN": PRACTICUM_TOKEN,
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        }.items()
        if not value
    ]

    if missing:
        message = (
            "Отсутствуют обязательные переменные окружения: "
            f"{', '.join(missing)}. Программа остановлена."
        )
        logger.critical(message)
        raise TokenError(message)


def send_message(bot: telebot.TeleBot, message: str) -> bool:
    """Отправляет сообщение в Telegram.

    Возвращает True при успешной отправке, False при ошибке.
    Логирует успешную отправку (DEBUG) или ошибку (ERROR).
    """
    try:
        logger.debug(
            "Попытка отправить сообщение в Telegram: %s",
            message,
        )
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.debug("Бот отправил сообщение: %s", message)
        return True
    except telebot.apihelper.ApiException as error:
        logger.error(
            "Ошибка Telegram API при отправке сообщения: %s",
            error,
        )
        return False
    except requests.RequestException as error:
        logger.error(
            "Сетевой сбой при отправке сообщения в Telegram: %s",
            error,
        )
        return False


def get_api_answer(timestamp: int) -> dict[str, Any]:
    """Делает запрос к API Практикума и возвращает ответ в виде словаря.

    В случае проблем с запросом или кода ответа != 200 возбуждает APIError.
    """
    params = {"from_date": timestamp}

    logger.debug(
        "Запрос к API: endpoint=%s, params=%s",
        ENDPOINT,
        params,
    )

    try:
        response = requests.get(
            ENDPOINT,
            headers=HEADERS,
            params=params,
            timeout=10,
        )
    except requests.RequestException as error:
        raise APIError(f"Ошибка запроса к API: {error}") from error

    if response.status_code != HTTPStatus.OK:
        raise APIError(
            f"Эндпоинт {ENDPOINT} вернул статус {response.status_code}"
        )

    try:
        return response.json()
    except ValueError as error:
        raise APIError(
            "Невозможно декодировать JSON в ответе API"
        ) from error


def check_response(response: Any) -> list[dict[str, Any]]:
    """Валидирует структуру ответа API и возвращает список домашних работ.

    Ожидается словарь с ключом 'homeworks' => список.
    При неверных типах возбуждает TypeError,
    при отсутствии ключа - ResponseError.
    """
    if not isinstance(response, dict):
        raise TypeError(
            f"Ожидался dict в ответе API, получен {type(response)}"
        )

    if "homeworks" not in response:
        raise ResponseError("В ответе API отсутствует ключ 'homeworks'")

    homeworks = response.get("homeworks")
    if not isinstance(homeworks, list):
        raise TypeError(
            f"'homeworks' должен содержать список, получен {type(homeworks)}"
        )

    return homeworks


def parse_status(homework: Any) -> str:
    """Извлекает статус ДР и формирует строку для отправки в Telegram.

    Ожидается, что homework - dict с ключами 'homework_name' и 'status'.
    """
    if not isinstance(homework, dict):
        raise ResponseError(
            f"Элемент homeworks должен быть словарём, получен {type(homework)}"
        )

    homework_name = homework.get("homework_name")
    if not homework_name:
        raise ResponseError(
            "В информации о домашней работе отсутствует 'homework_name'"
        )

    status = homework.get("status")
    if status is None:
        raise ResponseError(
            f"В информации о домашней работе "
            f"'{homework_name}' отсутствует 'status'"
        )

    verdict = HOMEWORK_VERDICTS.get(status)
    if verdict is None:
        raise StatusError(f"Неизвестный статус домашней работы: {status}")

    return f'Изменился статус проверки работы "{homework_name}". {verdict}'


def handle_homework(
    bot: telebot.TeleBot,
    homework: dict[str, Any],
) -> bool:
    """Обрабатывает домашнюю работу и отправляет сообщение."""
    message = parse_status(homework)

    if send_message(bot, message):
        return True

    logger.error("Не удалось отправить уведомление в Telegram")
    return False


def handle_error(
    bot: telebot.TeleBot,
    error: Exception,
    last_error_message: str | None,
) -> str | None:
    """Логирует и отправляет ошибки в Telegram один раз."""
    error_text = f"Сбой в работе программы: {error}"
    logger.error(error_text)

    if (
        str(error) != (last_error_message or "")
        and send_message(bot, error_text)
    ):
        return str(error)

    return last_error_message


def main() -> None:
    """Основная логика работы бота."""
    check_tokens()

    bot = telebot.TeleBot(TELEGRAM_TOKEN)
    timestamp = int(time.time())
    last_error_message: str | None = None

    logger.info("Бот запущен. Ожидание обновлений...")

    while True:
        try:
            response = get_api_answer(timestamp)
            homeworks = check_response(response)
            next_timestamp = int(response.get("current_date", timestamp))

            if homeworks:
                if handle_homework(bot, homeworks[0]):
                    timestamp = next_timestamp
            else:
                logger.debug("Отсутствие в ответе новых статусов")
                timestamp = next_timestamp

            last_error_message = None

        except (
            APIError,
            ResponseError,
            StatusError,
            TypeError,
            KeyError,
        ) as error:
            last_error_message = handle_error(
                bot,
                error,
                last_error_message,
            )
        except Exception as unexpected:
            last_error_message = handle_error(
                bot,
                unexpected,
                last_error_message,
            )
        finally:
            time.sleep(RETRY_PERIOD)


if __name__ == "__main__":
    try:
        main()
    except TokenError:
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (KeyboardInterrupt).")
        sys.exit(0)
    except Exception as error:
        logger.exception(
            "Бот завершился с необработанной ошибкой: %s",
            error,
        )
        sys.exit(1)
