import asyncio
import gradio as gr
import logfire
from pydantic_ai_agent_mcp_client import shutdown_logfire, chat_with_agent, agent


# Configure logger at the very beginning of the script
logfire.configure(service_name="gradio-chatbot-ui", send_to_logfire='if-token-present')
logfire.info("Starting Gradio Chatbot UI initialization...")


def create_chat_interface():
    """Create the chat interface using Gradio Blocks"""

    with gr.Blocks(
            title="AI Assistant",
            theme=gr.themes.Soft()
    ) as demo:
        gr.Markdown("# Chatbot with knowledge based on Huffington Post articles from science field (OpenAI Powered)")
        gr.Markdown("I can answer your question about science: ask questions or provide me with a link to an article.")

        # Chat components
        chatbot = gr.Chatbot(
            height=500,
            show_copy_button=True,
            container=True,
        )

        with gr.Row():
            msg = gr.Textbox(
                placeholder="Ask me about science (Huffington Post)!",
                container=False,
                scale=7,
                show_label=False
            )
            submit_btn = gr.Button("Send", scale=1)
            clear_btn = gr.Button("Clear", scale=1)

        # State for conversation history
        state = gr.State([])

        async def respond(message: str, history: list, state_data: list):
            """Handle user message and generate response"""
            if not message.strip():
                return history, state_data, ""

            try:
                # Add user message to history
                history = history + [[message, None]]

                # Get response from agent - collect the final response only
                response = ""
                async for response_chunk in chat_with_agent(message, state_data):
                    response = response_chunk  # Keep only the final accumulated response

                # Update the last message with the response
                history[-1][1] = response

                # Update state with agent's message history
                if hasattr(agent, 'last_run') and agent.last_run:
                    state_data = agent.last_run.all_messages()

                return history, state_data, ""

            except Exception as e:
                logfire.error(f"Error in chat processing: {str(e)}", exc_info=True)
                error_msg = f"Error: {str(e)}"
                if history and history[-1][1] is None:
                    history[-1][1] = error_msg
                else:
                    history = history + [[message, error_msg]]
                return history, state_data, ""

        def clear_chat():
            """Clear the chat history"""
            return [], [], ""

        # Event handlers
        msg.submit(
            respond,
            inputs=[msg, chatbot, state],
            outputs=[chatbot, state, msg],
        )

        submit_btn.click(
            respond,
            inputs=[msg, chatbot, state],
            outputs=[chatbot, state, msg],
        )

        clear_btn.click(
            clear_chat,
            outputs=[chatbot, state, msg],
        )

    return demo


if __name__ == "__main__":
    try:
        logfire.info("Starting Gradio chat interface...")
        demo = create_chat_interface()
        demo.queue(default_concurrency_limit=10)
        demo.launch(
            server_name="0.0.0.0",
            server_port=7860,
            share=False,
            show_error=True,
            debug=True,
        )
    except Exception as e:
        logfire.error(f"Failed to start interface: {str(e)}", exc_info=True)
    finally:
        logfire.info("Shutting down chat interface...")
        asyncio.run(shutdown_logfire())
