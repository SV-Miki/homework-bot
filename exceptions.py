"""Кастомные исключения для бота домашек Практикума."""


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
