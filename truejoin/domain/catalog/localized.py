"""Text in several languages."""

from pydantic import BaseModel, Field


class LocalizedText(BaseModel):
    """Multi-language text container.

    Stores text in multiple languages with fallback support.

    Example:
        text = LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"})
        text.get("tr")  # "Kullanıcılar"
        text.get("de", fallback="en")  # "Users"
    """

    texts: dict[str, str] = Field(default_factory=dict)

    def get(self, lang: str, fallback: str = "en") -> str:
        """Get text in specified language with fallback."""
        return self.texts.get(lang) or self.texts.get(fallback, "")

    def set(self, lang: str, text: str) -> None:
        """Set text for a specific language."""
        self.texts[lang] = text

    @property
    def is_empty(self) -> bool:
        """Check if no translations exist."""
        return not any(v.strip() for v in self.texts.values())

    @property
    def languages(self) -> list[str]:
        """Get list of available languages."""
        return list(self.texts.keys())

    def __str__(self) -> str:
        """Return first available text."""
        for text in self.texts.values():
            if text.strip():
                return text
        return ""
