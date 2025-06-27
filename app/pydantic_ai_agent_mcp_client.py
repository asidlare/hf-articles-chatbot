import os
import json
from typing import AsyncGenerator
import logfire
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.mcp import MCPServerSSE
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from system_prompt import SYSTEM_PROMPT


# Configure logger
logfire.configure(
    service_name="pydantic-ai-agent-service",
    send_to_logfire='if-token-present',
)
logfire.instrument_pydantic_ai(include_content=True, include_binary_content=True)
logfire.instrument_httpx(capture_all=True)
logfire.instrument_openai()
logfire.info("Starting PydanticAI Agent initialization...")


# model configuration
api_key = os.getenv("OPENAI_API_KEY")
model = OpenAIModel(
    'gpt-4.1-mini',
    provider=OpenAIProvider(api_key=api_key)
)


class InstrumentedMCPServerSSE(MCPServerSSE):
    """
    Extends the functionality of MCPServerSSE by adding logging and telemetry
    instrumentation for operations such as tool invocation and tool listing. This
    class aims to provide enhanced observability and debugging capabilities, ensuring
    better insights into the usage of MCP tools and applications.

    :ivar _server_url: The URL of the MCP server being interacted with. Defaults to
        'unknown' if not provided in the initialization arguments.
    :type _server_url: str
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._server_url = kwargs.get('url', 'unknown')

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """
        Calls a specified tool with given arguments and logs the operation, including
        success or failure details. This method extends functionality by incorporating
        logging for debugging and monitoring purposes, tracking tool calls, results, and
        any errors that occur during execution.

        :param tool_name: The name of the tool to be called.
        :type tool_name: str
        :param arguments: A dictionary containing arguments to be passed to the tool.
        :type arguments: dict
        :return: A dictionary containing the result of the tool execution.
        :rtype: dict
        :raises Exception: Propagates exceptions raised during tool execution, logging
            them for debugging.
        """
        with logfire.span(
                "mcp_tool_call",
                tool_name=tool_name,
                server_url=self._server_url,
                arguments=arguments
        ) as span:
            try:
                # Log the tool call start
                logfire.info(
                    f"MCP Tool Call: {tool_name} with arguments: {arguments}",
                    tool_name=tool_name,
                    server_url=self._server_url,
                    arguments=arguments,
                    arguments_size=len(json.dumps(arguments))
                )

                # Call the parent method
                result = await super().call_tool(tool_name, arguments)

                # Log the successful result
                result_size = len(json.dumps(result)) if isinstance(result, (dict, list)) else len(str(result))
                span.set_attributes({
                    "success": True,
                    "result_size": result_size,
                    "result_type": type(result).__name__
                })

                logfire.info(
                    f"MCP Tool Success: {tool_name}",
                    tool_name=tool_name,
                    result_size=result_size,
                    result_type=type(result).__name__,
                    result_preview=str(result)[:200] + "..." if len(str(result)) > 200 else str(result)
                )

                return result

            except Exception as e:
                # Log the error
                span.set_attributes({
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__
                })

                logfire.error(
                    f"MCP Tool Error: {tool_name}",
                    tool_name=tool_name,
                    server_url=self._server_url,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
                raise

    async def list_tools(self) -> list:
        """
        Lists all MCP tools available from the configured server URL.

        This method communicates with the MCP server to retrieve the available tools.
        It logs the process of fetching and formatting the tool information, including
        handling exceptions gracefully by logging errors and returning an empty list
        if anything goes wrong.

        :return: A list of available MCP tools. Each tool is represented either by its
            name if it has one, or by its string representation if the name is not
            available.
        :rtype: list
        """
        with logfire.span("mcp_list_tools", server_url=self._server_url):
            try:
                logfire.info(f"Listing MCP tools from {self._server_url}")

                tools = await super().list_tools()

                # Handle ToolDefinition objects properly
                tool_names = []
                for tool in tools:
                    # ToolDefinition objects have a 'name' attribute
                    if hasattr(tool, 'name'):
                        tool_names.append(tool.name)
                    else:
                        tool_names.append(str(tool))

                logfire.info(
                    f"MCP Tools Listed: {len(tools)} tools available",
                    server_url=self._server_url,
                    tools_count=len(tools),
                    tool_names=tool_names
                )

                return tools

            except Exception as e:
                logfire.error(
                    f"Failed to list MCP tools from {self._server_url}",
                    server_url=self._server_url,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
                # Return empty list for graceful degradation
                return []


# Define the URL of your external MCP tool server
EXTERNAL_MCP_SERVER_URL = os.getenv("HF_SEARCH_MCP_SERVER_URL")
mcp_server = InstrumentedMCPServerSSE(
    url=EXTERNAL_MCP_SERVER_URL,
    timeout=30,  # Increase timeout
    sse_read_timeout=600  # Increase SSE timeout
)

# Initialize the PydanticAI Agent with an OpenAI model
agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    mcp_servers=[mcp_server],
)


async def chat_with_agent(prompt: str, message_history: list[ModelMessage]) -> AsyncGenerator[str, None]:
    """
    Processes a chat prompt by interacting with an agent and yields the generated
    response text. The interaction includes logging, handling agent communication,
    and managing any errors that arise during the conversation workflow.

    This function manages the connection lifecycle with "MCP tool servers" using
    a context manager to ensure clean execution. It processes the user prompt
    asynchronously with the agent and logs the result details, including response
    length and content preview. Only one response is expected to be yielded.

    The function gracefully handles communication or unexpected errors by logging
    the issues and yielding appropriate error messages.

    :param prompt: The chat input prompt provided by the user.
    :type prompt: str
    :param message_history: The historical context of prior messages in the session.
    :type message_history: list[ModelMessage]
    :return: Asynchronous generator yielding the agent's response text as a string.
    :rtype: AsyncGenerator[str, None]
    """
    logfire.debug(f"Processing chat prompt: '{prompt}'")

    try:
        # Use the run_mcp_servers context manager for HTTP-based MCP servers
        async with agent.run_mcp_servers():
            with logfire.span("chat_with_agent", prompt=prompt):
                # Use run() to get a single response
                result = await agent.run(
                    user_prompt=prompt,
                    message_history=message_history
                )

                # Get the final response data
                response_text = result.data if hasattr(result, 'data') else str(result)

                # Log response details
                logfire.info(
                    "Generated final response",
                    response_length=len(response_text),
                    response_preview=response_text[:200] + "..." if len(response_text) > 200 else response_text
                )

                # Yield only once
                yield response_text

        logfire.info(
            "Chat processing completed",
            response_length=len(response_text),
            prompt=prompt[:50] + "..." if len(prompt) > 50 else prompt
        )

    except AgentRunError as e:
        error_msg = f"Error communicating with MCP tool server: {str(e)}"
        logfire.error(error_msg, exc_info=True)
        yield f"{error_msg}"

    except Exception as e:
        error_msg = f"Unexpected error in chat processing: {str(e)}"
        logfire.error(error_msg, exc_info=True)
        yield f"{error_msg}"


# Add a shutdown hook to ensure all spans/logs are sent to Logfire
# This is crucial in short-lived scripts or when the app exits.
async def shutdown_logfire():
    logfire.info("Shutting down Logfire for pydantic_ai_agent_mcp_client.")
    await logfire.shutdown()
