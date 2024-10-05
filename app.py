import base64
import json

import openai
from dotenv import load_dotenv
import chainlit as cl
from agents.base_agent import Agent

load_dotenv()

# Note: If switching to LangSmith, uncomment the following, and replace @observe with @traceable
# from langsmith.wrappers import wrap_openai
# from langsmith import traceable
# client = wrap_openai(openai.AsyncClient())

from langfuse.decorators import observe
from langfuse.openai import AsyncOpenAI

client = openai.AsyncClient()

gen_kwargs = {
    "model": "gpt-4o",
    "temperature": 0.2
}

SYSTEM_PROMPT = """\
You are an intelligent agent orchestrating the planning and implementation of a web page project. Your role is to ensure the milestones from artifacts/plan.md are handled efficiently by invoking the appropriate agent for each task.

You have two agents available for this process:

1. Planning Agent: Responsible for creating the plan and milestones based on an image.
2. Implementation Agent: Responsible for implementing a milestone by updating index.html and styles.css based on the 
plan in artifacts/plan.md.

Here are your guidelines for invoking these agents:

Invoke the Planning Agent when the user submits an image to generate a plan. If a plan already exists in artifacts/plan.md, do not invoke the Planning Agent unless the user requests an update or revision.
Invoke the Implementation Agent to implement a milestone from the artifacts/plan.md file. The Implementation Agent works on one milestone at a time and marks it as completed after updating index.html and styles.css. If feedback is provided, the Implementation Agent should update the necessary files accordingly.
Function Calling Instructions:

When appropriate, respond with the function call in JSON format without executing it. The structure of your response should be:

{
  "function_name": "function_name_here",
  "parameters": [list_of_arguments]
}


call_agent('planning'): When the user submits an image for planning or requests a plan update.

example: 

{
  "function_name": "call_agent",
  "parameters": ["planning"]
}

call_agent('implementation'): When a milestone from the plan needs to be implemented.

example: 

{
  "function_name": "call_agent",
  "parameters": ["implementation"]
}

Ensure the following:

If the plan doesn't exist or is incomplete, prioritize invoking the Planning Agent to generate or update the milestones.
If the plan exists and is valid, invoke the Implementation Agent to work on the next milestone in sequence.
Coordinate between agents, ensuring that feedback on implementation is passed back to the Implementation Agent, and that plans are kept up-to-date.
Your goal is to collaborate between the agents, ensuring the project progresses smoothly from planning to implementation.
"""

PLANNING_PROMPT = """\
You are a software architect, preparing to build the web page in the image that the user sends. 
Once they send an image, generate a plan, described below, in markdown format.

If the user or reviewer confirms the plan is good, available tools to save it as an artifact \
called `plan.md`. If the user has feedback on the plan, revise the plan, and save it using \
the tool again. A tool is available to update the artifact. Your role is only to plan the \
project. You will not implement the plan, and will not write any code.

If the plan has already been saved, no need to save it again unless there is feedback. Do not \
use the tool again if there are no changes.

For the contents of the markdown-formatted plan, create two sections, "Overview" and "Milestones".

In a section labeled "Overview", analyze the image, and describe the elements on the page, \
their positions, and the layout of the major sections.

Using vanilla HTML and CSS, discuss anything about the layout that might have different \
options for implementation. Review pros/cons, and recommend a course of action.

In a section labeled "Milestones", describe an ordered set of milestones for methodically \
building the web page, so that errors can be detected and corrected early. Pay close attention \
to the aligment of elements, and describe clear expectations in each milestone. Do not include \
testing milestones, just implementation.

Milestones should be formatted like this:

 - [ ] 1. This is the first milestone
 - [ ] 2. This is the second milestone
 - [ ] 3. This is the third milestone
"""

IMPLEMENTATION_PROMPT = """\
You are an Implementation Agent tasked with reading the plan.md file located in the artifacts/ folder and implementing ONE of the milestones at a time, as described in the plan. Your responsibilities include:

1. Reviewing the current milestone from artifacts/plan.md and marking it as "complete" once implemented.
2. Generating or updating the artifacts/index.html and artifacts/styles.css files to reflect the milestone’s progress.
3. Saving the updated project artifacts in the artifacts/ folder.
Follow these steps:

Read the artifacts/plan.md file to identify the next milestone to implement.
Generate or modify the necessary HTML and CSS code in small, incremental steps to complete the current milestone.
Save the updated artifacts (artifacts/index.html, artifacts/styles.css) and mark the milestone as completed in artifacts/plan.md.
Stop after completing the milestone and await further instructions or feedback.
If feedback is provided, modify the corresponding code and re-save the artifacts in the artifacts/ folder.

Use available tools to save artifacts.

Ensure the following:

Each step is small and focused on a specific part of the milestone.
Code should be clear and adhere to the specified design in the plan.
"""

# Create an instance of the Agent class
planning_agent = Agent(name="Planning Agent", client=client, prompt=PLANNING_PROMPT)

implementation_agent = Agent(name="Implementation Agent", client=client, prompt=IMPLEMENTATION_PROMPT)


# @observe
@cl.on_chat_start
def on_chat_start():
    message_history = [{"role": "system", "content": SYSTEM_PROMPT}]
    cl.user_session.set("message_history", message_history)


# @observe
async def generate_response(client, message_history, gen_kwargs):
    response_message = cl.Message(content="")
    await response_message.send()

    stream = await client.chat.completions.create(messages=message_history, stream=True, **gen_kwargs)
    async for part in stream:
        if token := part.choices[0].delta.content or "":
            await response_message.stream_token(token)

    await response_message.update()

    return response_message



@cl.on_message
# @observe
async def on_message(message: cl.Message):
    message_history = cl.user_session.get("message_history", [])

    # Processing images exclusively
    images = [file for file in message.elements if "image" in file.mime] if message.elements else []

    if images:
        # Read the first image and encode it to base64
        with open(images[0].path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode('utf-8')
        message_history.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": message.content
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                }
            ]
        })
    else:
        message_history.append({"role": "user", "content": message.content})


    response_message = await generate_response(client, message_history, gen_kwargs)

    content = response_message.content
    result = parse_and_invoke(content)
    if result is not None:
        response_message = await result.execute(message_history)

    message_history.append({"role": "assistant", "content": response_message})
    cl.user_session.set("message_history", message_history)


# Function to safely parse and invoke
def parse_and_invoke(json_string):
    try:
        # Parse the JSON string
        parsed = json.loads(json_string)

        # Extract the function name and parameters
        function_name = parsed.get("function_name")
        parameters = parsed.get("parameters", [])

        # Dynamically get the function by name
        func = globals().get(function_name)

        # Check if the function exists and invoke it with the parameters
        if func and callable(func):
            return func(*parameters)
        else:
            print(f"Function '{function_name}' not found or is not callable.")
            return None

    except (json.JSONDecodeError, TypeError, ValueError) as e:
        # Handle any parsing or invocation errors
        print(f"Error parsing or invoking function: {e}")
        return None


def call_agent(agent_type: str):
    if agent_type == "planning":
        return planning_agent
    else:
        return implementation_agent


if __name__ == "__main__":
    cl.main()
