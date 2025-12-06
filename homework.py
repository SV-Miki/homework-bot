import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional
from http import HTTPStatus

import requests
import telebot
from dotenv import load_dotenv

# локальные кастомные исключения
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

# Настройка логирования
logger = logging.getLogger("homework_logger")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(stream=sys.stdout)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)


def check_tokens() -> None:
    """Проверяет наличие обязательных переменных окружения.

    При отсутствии хотя бы одной переменной возбуждает TokenError.
    """
    missing = [
        name
        for name, val in {
            "PRACTICUM_TOKEN": PRACTICUM_TOKEN,
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        }.items()
        if not val
    ]

    if missing:
        message = (
            f"Отсутствует обязательная переменная(ые) окружения: "
            f"{', '.join(missing)}. Программа остановлена."
        )
        # Логируем критически и возбуждаем исключение
        logger.critical(message)
        raise TokenError(message)


def send_message(bot: telebot.TeleBot, message: str) -> bool:
    """Отправляет сообщение в Telegram.

    Возвращает True при успешной отправке, False при ошибке.
    Логирует успешную отправку (DEBUG) или ошибку (ERROR).
    """
    try:
        logger.debug(f"Попытка отправить сообщение в Telegram: {message}")
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.debug(f"Бот отправил сообщение: {message}")
        return True
    except telebot.apihelper.ApiException as api_err:
        # Ошибка со стороны Telegram API
        logger.error(f"Ошибка Telegram API при отправке сообщения: {api_err}")
        return False
    except requests.RequestException as req_err:
        # Ошибки, которые могут возникнуть из-за сетевого слоя
        logger.error(
            f"Сетевой сбой при отправке сообщения в Telegram: {req_err}"
        )
        return False
    except Exception as err:
        # На всякий случай ловим и другие исключения, логируем
        logger.error(
            f"Неожиданная ошибка при отправке сообщения в Telegram: {err}"
        )
        return False


def get_api_answer(timestamp: int) -> Dict[str, Any]:
    """Делает запрос к API Практикума и возвращает ответ в виде словаря.

    В случае проблем с запросом или кода ответа != 200 возбуждает APIError.
    """
    headers = {"Authorization": f"OAuth {PRACTICUM_TOKEN}"}
    params = {"from_date": timestamp}

    # Логируем факт запроса (кратко)
    logger.debug(f"Запрос к API: endpoint={ENDPOINT}, params={params}")

    try:
        response = requests.get(
            ENDPOINT, headers=headers, params=params, timeout=10
        )
    except requests.RequestException as error:
        # Логируем и шлём сообщение о проблеме в main
        raise APIError(f"Ошибка запроса к API: {error}") from error

    if response.status_code != HTTPStatus.OK:
        raise APIError(
            f"Эндпоинт {ENDPOINT} вернул статус {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as error:
        raise APIError("Невозможно декодировать JSON в ответе API") from error

    return data


def check_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
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


def parse_status(homework: Dict[str, Any]) -> str:
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
            f"В информации о д.р '{homework_name}' отсутствует 'status'"
        )

    verdict = HOMEWORK_VERDICTS.get(status)
    if verdict is None:
        raise StatusError(f"Неизвестный статус домашней работы: {status}")

    return f'Изменился статус проверки работы "{homework_name}". {verdict}'


def handle_homework(bot: telebot.TeleBot, homework: dict,
                    timestamp: int) -> int:
    """Обрабатывает домашнюю работу и отправляет сообщение.

    Возвращает новый timestamp.
    """
    message = parse_status(homework)
    if send_message(bot, message):
        return int(homework.get("current_date", timestamp))
    logger.error("Не удалось отправить уведомление в Telegram")
    return timestamp


def handle_error(bot: telebot.TeleBot, error: Exception,
                 last_error_message: Optional[str]) -> Optional[str]:
    """Логирует и отправляет ошибки в Telegram один раз."""
    err_text = f"Сбой в работе программы: {error}"
    logger.error(err_text)
    if str(error) != (last_error_message or ""):
        if send_message(bot, err_text):
            return str(error)
    return last_error_message


def main() -> None:
    """Основная логика работы бота."""
    check_tokens()

    bot = telebot.TeleBot(TELEGRAM_TOKEN)
    timestamp = int(time.time())
    last_error_message: Optional[str] = None

    logger.info("Бот запущен. Ожидание обновлений...")

    while True:
        try:
            response = get_api_answer(timestamp)
            homeworks = check_response(response)
            next_timestamp = int(response.get("current_date", timestamp))

            if homeworks:
                timestamp = handle_homework(bot, homeworks[0], timestamp)
            else:
                logger.debug("Отсутствие в ответе новых статусов")
                timestamp = next_timestamp

        except (APIError,
                ResponseError,
                StatusError,
                TypeError,
                KeyError) as error:
            last_error_message = handle_error(bot, error, last_error_message)
        except Exception as unexpected:
            last_error_message = handle_error(
                bot, unexpected, last_error_message
            )
        finally:
            time.sleep(RETRY_PERIOD)


if __name__ == "__main__":
    try:
        main()
    except TokenError:
        # сообщение уже залогировано внутри check_tokens/main
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (KeyboardInterrupt).")
        sys.exit(0)
    except Exception as exc:
        logger.exception(f"Бот завершился с необработанной ошибкой: {exc}")
        sys.exit(1)
