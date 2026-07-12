class IntentResult:
    def __init__(self, requires_tool=False, tool=None):
        self.requires_tool = requires_tool
        self.tool = tool


class IntentRouter:
    def __init__(self, llm=None):
        pass

    async def classify(self, text: str):
        t = text.lower()

        if "weather" in t:
            return IntentResult(True, "weather")

        if "news" in t or "search" in t:
            return IntentResult(True, "search")

        return IntentResult(False)

    async def plan(self, text, messages):
        return {"text": text}