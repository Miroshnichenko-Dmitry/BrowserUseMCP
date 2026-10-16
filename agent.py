import asyncio
from fastmcp import FastMCP
from browser_use import Agent, Browser, ChatBrowserUse, ChatOpenAI
from browser_use.llm.ollama.chat import ChatOllama
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("browser-use-agent")

@mcp.tool()
async def run_browser_task(task: str) -> str:
    """Запускает browser-use агента с задачей на естественном языке."""
    llm = ChatOllama(
        model='qwen3.5:4b',
        host='http://localhost:11434'
    )
    # llm = ChatOpenAI(model='gpt-5.6-luna', reasoning_effort='xhigh')
    # llm = ChatBrowserUse(model='bu-2-0')  # Use BU2 instead; requires BROWSER_USE_API_KEY
    agent = Agent(
        task=task,
        llm=llm,
        # browser=Browser(use_cloud=True),  # Use a cloud browser; requires BROWSER_USE_API_KEY
    )
    history = await agent.run()
    print(history.final_result())

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)