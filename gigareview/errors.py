"""Исключения gigareview: всё, что демон умеет показать пользователю понятным текстом."""

__all__ = ["BitbucketError", "ConfigError", "GigaReviewError", "GitError", "SetupError"]


class GigaReviewError(Exception):
    """Базовая ошибка gigareview: текст исключения пригоден для журнала и Web Dashboard."""


class ConfigError(GigaReviewError):
    """Конфигурация не найдена, не разбирается или противоречива."""


class GitError(GigaReviewError):
    """Команда git завершилась ошибкой или не уложилась в таймаут."""


class SetupError(GigaReviewError):
    """Подготовка прогона не удалась: команда init, неизвестный репозиторий, нет сценариев."""


class BitbucketError(GigaReviewError):
    """Ошибка обращения к Bitbucket: сеть или HTTP-статус ответа."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Ошибка с текстом ``message`` и HTTP-статусом (``None`` — ошибка сети)."""
        super().__init__(message)
        self.status_code = status_code
