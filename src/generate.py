# 生成答案
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
from retrieve import retrieve

import csv
import json


class AnswerDraft(BaseModel):
    answer: str
    answer_value: str
    ref_ids: list[str]
    supporting_materials: str
    explanation: str


prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
Answer the question using only the supplied evidence.

Return a JSON object with exactly these keys:
answer, answer_value, ref_ids, supporting_materials, explanation.

Rules:
1. answer contains the readable answer.
2. answer_value contains the actual answer used for scoring:
   - For a numerical answer, return the number without its unit.
   - For a name or category, return that name or category.
   - For a true/false question, return "1" for true or "0" for false.
3. A missing expected unit does NOT mean the question is unanswerable.
4. Cite only ref_ids present in the evidence that directly support
   the answer. Do not cite unrelated evidence.
5. supporting_materials contains quotations supporting the answer.
   Do not invent quotations.
6. If calculation is needed, calculate from numbers supplied in
   the evidence and explain the calculation. Do not invent missing
   quantities.
7. If the evidence is insufficient, return:
   answer = "is_blank"
   answer_value = "is_blank"
   ref_ids = []
   supporting_materials = "is_blank"
   explanation = a brief explanation of what evidence is missing.
8. explanation must always be non-empty.
9. Keep answer and answer_value consistent.

Example for a categorical answer with no unit:
{{"answer":"Example Model",
  "answer_value":"Example Model",
  "ref_ids":["paper_id"],
  "supporting_materials":"An exact supporting quotation.",
  "explanation":"The quotation identifies the requested model."}}
""",
    ),
    (
        "human",
        "Question: {question}\n"
        "Expected unit: {answer_unit}\n\n"
        "Evidence:\n{context}",
    ),
])

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise RuntimeError("Set DEEPSEEK_API_KEY in the project .env file")

llm = ChatOpenAI(
    model="deepseek-chat",
    api_key=api_key,
    base_url="https://api.deepseek.com",
    temperature=0,
).with_structured_output(AnswerDraft, method="json_mode")


def generate_answer(question: str, answer_unit: str, docs):
    context = "\n\n".join(
        f"[ref_id: {doc.metadata['ref_id']}]\n{doc.page_content}"
        for doc in docs
    )

    unit_hint = (
        "No unit; answer_value must still contain the answer."
        if answer_unit.strip().lower() == "is_blank"
        else answer_unit
    )

    messages = prompt.invoke({
        "question": question,
        "answer_unit": unit_hint,
        "context": context,
    })

    result = llm.invoke(messages)
    return result.model_dump()


def answer_one(row, metadata_by_id):
    docs = retrieve(row["question"])
    draft = generate_answer(row["question"], row["answer_unit"], docs)
    # 只接受本题实际检索到的文档 ID，并去重。

    available_ids = {doc.metadata["ref_id"] for doc in docs}
    ref_ids = list(dict.fromkeys(
        ref_id for ref_id in draft["ref_ids"]
        if ref_id in available_ids
    ))

    answer_value = draft["answer_value"].strip()
    is_unanswerable = answer_value.lower() == "is_blank"

    if is_unanswerable:
        answer_value = "is_blank"
        ref_ids = []

    if not draft["explanation"].strip():
        raise ValueError(f"{row['id']} 缺少 explanation")

    submission_row = dict(row)  # 保留 id、question、answer_unit、Cohort
    submission_row.update({
        "answer": "is_blank" if is_unanswerable else draft["answer"],
        "answer_value": answer_value,
        "ref_id": json.dumps(ref_ids) if ref_ids else "is_blank",
        "ref_url": (
            json.dumps([metadata_by_id[ref_id]["url"] for ref_id in ref_ids])
            if ref_ids else "is_blank"
        ),
        "supporting_materials": (
            "is_blank" if is_unanswerable else draft["supporting_materials"]
        ),
        "explanation": draft["explanation"],
    })
    return submission_row


def predict_all():
    # 读取 input/test_Q.csv 和 input/metadata.csv
    # 逐行调用 answer_one()
    # 写出 submissions/test_submission.csv

    input_dir = Path("./input")
    output_dir = Path("./submissions")
    output_dir.mkdir(exist_ok=True)

    with (input_dir / "metadata.csv").open(
            encoding="utf-8-sig", newline=""
    ) as file:
        metadata_by_id = {
            row["id"]: row for row in csv.DictReader(file)
        }

    with (input_dir / "test_Q.csv").open(
            encoding="utf-8-sig", newline=""
    ) as file:
        reader = csv.DictReader(file)
        fieldnames = reader.fieldnames
        predictions = [
            answer_one(row, metadata_by_id)
            for row in reader
        ]

    output_path = output_dir / "test_submission.csv"
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(predictions)

    return output_path
