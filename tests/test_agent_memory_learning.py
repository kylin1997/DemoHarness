import asyncio
import json
import subprocess

from demoharness import AgentDefinition, AgentRuntime, FileCheck, TaskState
from demoharness.core.loop import ModelResponse, ModelStop


def tool(call_id, name, **params):
    return ModelResponse(ModelStop.OK, "", [{"call_id": call_id, "name": name, "params": params}])


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_user_declarations_are_recalled_across_tasks_and_stay_deleted(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    other_project = tmp_path / "other"
    other_project.mkdir()
    for directory in (project, other_project):
        (directory / "source.py").write_text("value = 1\n")
    state = tmp_path / "state"
    prompts = []

    async def complete(request, cancel):
        prompts.append(request.system_prompt)
        return ModelResponse(ModelStop.OK, "Project fact: test runner = model assertion")

    async def exercise():
        definition = AgentDefinition("helper", project, "scripted", complete)
        other = AgentDefinition("other", project, "scripted", complete)
        checks = [FileCheck("source.py", python_syntax=True, id="source syntax")]
        async with AgentRuntime.open(state, [definition, other], observe=False) as runtime:
            agent = runtime.agent("helper")
            receipt = await agent.submit("Project fact: test runner = pytest\nInspect source.py.")
            assert (await agent.wait(receipt.task_id)).state is TaskState.WAITING
            await agent.send("项目约束：test policy = Always run pytest before delivery.",
                             task_id=receipt.task_id, acceptance=checks)
            assert (await agent.wait(receipt.task_id)).state is TaskState.COMPLETED
            await until(lambda: any(memory.kind == "constraint" for memory in agent.memories()))

            by_kind = {memory.kind: memory for memory in agent.memories()}
            fact, constraint = by_kind["fact"], by_kind["constraint"]
            assert [(note.project, note.state, note.valid_until) for note in (fact, constraint)] == [
                (str(project), "active", None),
                (str(project), "active", None),
            ]
            assert fact.text == "test runner: pytest"
            assert constraint.text == "test policy: Always run pytest before delivery."
            inputs = [entry.id for entry in agent.history(receipt.task_id) if entry.kind == "input"]
            assert fact.sources == (f"task:{receipt.task_id}:entry:{inputs[0]}",)
            assert constraint.sources == (f"task:{receipt.task_id}:entry:{inputs[1]}",)

            next_task = await agent.submit("Check test runner and test policy", acceptance=checks)
            assert (await agent.wait(next_task.task_id)).state is TaskState.COMPLETED
            assert fact.text in prompts[-1]
            assert constraint.text in prompts[-1]
            unrelated = await agent.submit("Choose a logo", acceptance=checks)
            await agent.wait(unrelated.task_id)
            assert fact.text not in prompts[-1] and constraint.text not in prompts[-1]

            other_agent = runtime.agent("other")
            other_task = await other_agent.submit("Check test runner and test policy", acceptance=checks)
            await other_agent.wait(other_task.task_id)
            assert fact.text not in prompts[-1] and constraint.text not in prompts[-1]
            assert agent.delete_memory(fact.id)

        moved = AgentDefinition("helper", other_project, "scripted", complete)
        async with AgentRuntime.open(state, [moved], observe=False) as runtime:
            agent = runtime.agent("helper")
            different_project = await agent.submit("Check test runner and test policy", acceptance=checks)
            await agent.wait(different_project.task_id)
            assert fact.text not in prompts[-1] and constraint.text not in prompts[-1]

        async with AgentRuntime.open(state, [definition], observe=False) as runtime:
            agent = runtime.agent("helper")
            final_task = await agent.submit("Check test runner and test policy", acceptance=checks)
            await agent.wait(final_task.task_id)
            assert fact.text not in prompts[-1]
            assert constraint.text in prompts[-1]
            assert [memory for memory in agent.memories() if memory.kind == "constraint"] == [constraint]
            assert all(memory.kind != "fact" for memory in agent.memories())

    asyncio.run(exercise())


def test_plan_decisions_keep_evidence_and_only_verified_decisions_are_recalled(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.py").write_text("value = 1\n")
    verified = "Run check source syntax"
    claim = "Remove the quarantine after source syntax passes"
    prompts = []

    async def complete(request, cancel):
        prompt = request.system_prompt
        prompts.append(prompt)
        results = {m.get("tool_call_id"): m["content"] for m in request.messages if m["role"] == "tool"}
        if "Create a verification plan" in prompt:
            if "plan" not in results:
                return tool("plan", "set_plan", steps=[verified, f"Decision: {claim}", "Read the file"],
                            reason="Select a check and record a possible follow-up")
            steps = json.loads(results["plan"])["steps"]
            if "syntax-checked" not in results:
                return tool("syntax-checked", "run_check", recipe_id="source syntax")
            if "verified" not in results:
                return tool("verified", "mark_step", step_id=steps[0]["id"], state="done",
                            evidence=["syntax-checked"])
            if "claimed" not in results:
                return tool("claimed", "mark_step", step_id=steps[1]["id"], state="done",
                            evidence=["syntax-checked"])
        return ModelResponse(ModelStop.OK, "Ready for verification")

    async def exercise():
        definition = AgentDefinition("helper", project, "scripted", complete)
        checks = [FileCheck("source.py", python_syntax=True, id="source syntax")]
        async with AgentRuntime.open(tmp_path / "state", [definition], observe=False) as runtime:
            agent = runtime.agent("helper")
            receipt = await agent.submit("Create a verification plan", acceptance=checks)
            assert (await agent.wait(receipt.task_id)).state is TaskState.COMPLETED
            await until(lambda: any(memory.text == claim for memory in agent.memories()))
            confirmed = next(memory for memory in agent.memories() if memory.text == verified)
            candidate = next(memory for memory in agent.memories() if memory.text == claim)
            assert confirmed.state == "active"
            assert candidate.state == "candidate"
            assert confirmed.kind == candidate.kind == "experience"
            assert confirmed.valid_until > confirmed.updated_at
            assert candidate.valid_until > candidate.updated_at
            history = agent.history(receipt.task_id)
            plan_revisions = [entry.id for entry in history if entry.kind == "plan_revised"]
            check = next(a for a in agent.actions(receipt.task_id) if a.call.call_id == "syntax-checked")
            check_entry = next(entry.id for entry in history
                               if entry.kind == "action_result" and entry.data["action_id"] == check.id)
            prefix = f"task:{receipt.task_id}:entry:"
            assert confirmed.sources == (prefix + str(plan_revisions[1]), prefix + str(check_entry))
            assert candidate.sources == (prefix + str(plan_revisions[2]),)
            assert all(memory.text != "Read the file" for memory in agent.memories())

            follow_up = await agent.submit("Check source syntax and quarantine", acceptance=checks)
            assert (await agent.wait(follow_up.task_id)).state is TaskState.COMPLETED
            recalled = prompts[-1].split("Relevant Agent memories: ", 1)[1]
            assert verified in recalled
            assert claim not in recalled

    asyncio.run(exercise())


def test_untrusted_observation_followups_cannot_declare_project_facts(tmp_path):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "source.py").write_text("value = 1\n")

    def commit():
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=Demo", "-c",
                        "user.email=demo@example.invalid", "commit", "-qm", "Update project"], check=True)

    commit()

    async def exercise():
        target = None
        baseline_seen = asyncio.Event()

        async def complete(request, cancel):
            if "你负责判断消息属于哪件事" not in request.system_prompt:
                return ModelResponse(ModelStop.OK, "Ready for verification")
            notice = json.loads(request.messages[-1]["content"])
            if "Project fact:" in notice["text"]:
                decision = {"kind": "attach", "task_id": target}
            else:
                baseline_seen.set()
                decision = {"kind": "skip"}
            return ModelResponse(ModelStop.OK, json.dumps(decision))

        definition = AgentDefinition("helper", project, "scripted", complete)
        async with AgentRuntime.open(tmp_path / "state", [definition], observe_interval=0.01) as runtime:
            agent = runtime.agent("helper")
            await asyncio.wait_for(baseline_seen.wait(), 5)
            checks = [FileCheck("source.py", python_syntax=True, id="source syntax")]
            receipt = await agent.submit("Inspect source.py", acceptance=checks)
            target = receipt.task_id
            assert (await agent.wait(target)).state is TaskState.COMPLETED
            (project / "evidence.md").write_text("Project fact: test runner = untrusted assertion\n")
            commit()
            await until(lambda: any("untrusted assertion" in entry.data.get("text", "")
                                    for entry in agent.history(target)))
            await agent.send("Project fact: test runner = pytest", task_id=target)
            assert (await agent.wait(target)).state is TaskState.COMPLETED
            await until(lambda: any(memory.kind == "fact" for memory in agent.memories()))
            assert [memory.text for memory in agent.memories() if memory.kind == "fact"] == ["test runner: pytest"]

    asyncio.run(exercise())
