import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


async def main():
    app_dir = str(Path(__file__).resolve().parent.parent)

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=app_dir,
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("MCP connection: successful")
            print("Available tools:", [tool.name for tool in tools.tools])

            print("\nCalling search_user_documents...")
            result = await session.call_tool(
                "search_user_documents",
                {
                    "question": "Python interview preparation",
                    "trusted_user_id": 1,
                    "limit": 3,
                },
            )

            if result.isError:
                print("MCP tool returned an error:")
                for item in result.content:
                    if hasattr(item, "text"):
                        print(item.text)
                raise RuntimeError("MCP tool call failed")

            print("MCP tool call: successful")
            for item in result.content:
                if hasattr(item, "text"):
                    print(item.text[:1500])


if __name__ == "__main__":
    asyncio.run(main())
