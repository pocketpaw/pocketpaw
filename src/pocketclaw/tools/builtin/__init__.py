# Builtin tools package.
# Changes:
#   - Added BrowserTool export
#   - 2026-02-05: Added RememberTool, RecallTool for memory
#   - 2026-02-06: Added WebSearchTool, ImageGenerateTool, CreateSkillTool
#   - 2026-02-07: Added Gmail, Calendar, Voice, Research, Delegate tools
#   - 2026-02-09: Added STT, Drive, Docs, Spotify, OCR, Reddit tools
#   - 2026-02-09: Converted to lazy __getattr__ to avoid ImportError when optional deps missing

import importlib as _importlib

# Map exported names to their (module, name) within this package.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "ShellTool": (".shell", "ShellTool"),
    "ReadFileTool": (".filesystem", "ReadFileTool"),
    "WriteFileTool": (".filesystem", "WriteFileTool"),
    "ListDirTool": (".filesystem", "ListDirTool"),
    "BrowserTool": (".browser", "BrowserTool"),
    "RememberTool": (".memory", "RememberTool"),
    "RecallTool": (".memory", "RecallTool"),
    "ForgetTool": (".memory", "ForgetTool"),
    "WebSearchTool": (".web_search", "WebSearchTool"),
    "UrlExtractTool": (".url_extract", "UrlExtractTool"),
    "ImageGenerateTool": (".image_gen", "ImageGenerateTool"),
    "CreateSkillTool": (".skill_gen", "CreateSkillTool"),
    "GmailSearchTool": (".gmail", "GmailSearchTool"),
    "GmailReadTool": (".gmail", "GmailReadTool"),
    "GmailSendTool": (".gmail", "GmailSendTool"),
    "GmailListLabelsTool": (".gmail", "GmailListLabelsTool"),
    "GmailCreateLabelTool": (".gmail", "GmailCreateLabelTool"),
    "GmailModifyTool": (".gmail", "GmailModifyTool"),
    "GmailTrashTool": (".gmail", "GmailTrashTool"),
    "GmailBatchModifyTool": (".gmail", "GmailBatchModifyTool"),
    "CalendarListTool": (".calendar", "CalendarListTool"),
    "CalendarCreateTool": (".calendar", "CalendarCreateTool"),
    "CalendarPrepTool": (".calendar", "CalendarPrepTool"),
    "TextToSpeechTool": (".voice", "TextToSpeechTool"),
    "SpeechToTextTool": (".stt", "SpeechToTextTool"),
    "ResearchTool": (".research", "ResearchTool"),
    "DelegateToClaudeCodeTool": (".delegate", "DelegateToClaudeCodeTool"),
    "DriveListTool": (".gdrive", "DriveListTool"),
    "DriveDownloadTool": (".gdrive", "DriveDownloadTool"),
    "DriveUploadTool": (".gdrive", "DriveUploadTool"),
    "DriveShareTool": (".gdrive", "DriveShareTool"),
    "DocsReadTool": (".gdocs", "DocsReadTool"),
    "DocsCreateTool": (".gdocs", "DocsCreateTool"),
    "DocsSearchTool": (".gdocs", "DocsSearchTool"),
    "SpotifySearchTool": (".spotify", "SpotifySearchTool"),
    "SpotifyNowPlayingTool": (".spotify", "SpotifyNowPlayingTool"),
    "SpotifyPlaybackTool": (".spotify", "SpotifyPlaybackTool"),
    "SpotifyPlaylistTool": (".spotify", "SpotifyPlaylistTool"),
    "OCRTool": (".ocr", "OCRTool"),
    "RedditSearchTool": (".reddit", "RedditSearchTool"),
    "RedditReadTool": (".reddit", "RedditReadTool"),
    "RedditTrendingTool": (".reddit", "RedditTrendingTool"),
    "NewSessionTool": (".sessions", "NewSessionTool"),
    "ListSessionsTool": (".sessions", "ListSessionsTool"),
    "SwitchSessionTool": (".sessions", "SwitchSessionTool"),
    "ClearSessionTool": (".sessions", "ClearSessionTool"),
    "RenameSessionTool": (".sessions", "RenameSessionTool"),
    "DeleteSessionTool": (".sessions", "DeleteSessionTool"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        module = _importlib.import_module(module_path, __package__)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_LAZY_IMPORTS.keys())
