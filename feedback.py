import chainlit as cl

from orchestrator_client import call_orchestrator_for_feedback

def create_feedback_actions(question_id: str, conversation_id: str, ask: str) -> list:
    """Create feedback actions for a message"""

    return [
        cl.Action(
            name="show_feedback_form",
            payload={"questionId": question_id, "conversationId": conversation_id,"ask": ask,"is_positive": 1},
            label="👍",
            description="Give detailed feedback"
        ),
        cl.Action(
            name="show_feedback_form",
            payload={"questionId": question_id, "conversationId": conversation_id,"ask": ask,"is_positive": 0},
            label="👎",
            description="Give detailed feedback"
        )
    ] # if os.getenv("ENABLE_FEEDBACK", "true").lower() == "true" else []


def register_feedback_handlers(auth_info= None):
    """Register feedback-related Chainlit event handlers"""

    feedback_msg: cl.Message | None = None

    @cl.action_callback("show_feedback_form")
    async def on_show_feedback_form(action):
        """Show feedback form action"""
        question_id = action.payload.get("questionId","dummy-question-id")
        conversation_id = action.payload.get("conversationId","dummy-conversation-id")
        feedback_element = cl.CustomElement(
            name="FeedbackForm", 
            props={
                "conversationId": conversation_id,
                "questionId": question_id,
                "ask": action.payload.get("ask"),
                "isPositive": action.payload.get("is_positive"),
                "feedbackType": "general",
                "show": True
            },
            display="inline"
        )

        global feedback_msg
        feedback_msg = cl.Message(
            content="",
            elements=[feedback_element]
        )
        await feedback_msg.send()

    @cl.action_callback("submit_feedback")
    async def handle_feedback(action):
        """Handle feedback submission"""
        try:
            question_id = action.payload.get("questionId")
            ask = action.payload.get("ask")
            conversation_id = action.payload.get("conversationId")
            text = action.payload.get("text","")
            rating = action.payload.get("rating")
            is_positive = action.payload.get("isPositive")
            global feedback_msg

            # Validate required fields
            if not all([conversation_id, rating]):
                raise ValueError("[app] Conversation ID and rating are required.")

            # Call orchestrator
            orc_feedback_response = await call_orchestrator_for_feedback(
                conversation_id=conversation_id,
                question_id=question_id,
                ask= ask,
                is_positive=is_positive,
                star_rating=rating,
                feedback_text=text,
                auth_info=auth_info() if auth_info else {}
            )
            # Remove the feedback form message
            if feedback_msg is not None:
                await feedback_msg.remove()
                feedback_msg = None

            # Send appropriate response
            if orc_feedback_response:
                return await cl.context.emitter.send_toast( "Thank you for your feedback!","success")
            else:
                return await cl.context.emitter.send_toast( "Error: Failed to submit feedback","error")

        except Exception as e:
            if feedback_msg is not None:
                await feedback_msg.remove()
                feedback_msg = None
            return await cl.context.emitter.send_toast( f"An unexpected error occurred while submitting feedback: {e}","error")


    @cl.action_callback("close_feedback_popup")
    async def close_feedback_handler(action):
        global feedback_msg
        if feedback_msg is not None:
            await feedback_msg.remove()
            feedback_msg = None
