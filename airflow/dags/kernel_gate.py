"""DAG responsável por ler dados de bases ISIS e replica-los
em uma REST-API que implementa a específicação do SciELO Kernel"""

import os
import requests
import json
import http.client
from typing import List
from airflow import DAG
from airflow import exceptions
from airflow.models import Variable
from airflow.operators.bash_operator import BashOperator
from airflow.operators.python_operator import PythonOperator
from airflow.hooks.http_hook import HttpHook
from xylose.scielodocument import Journal, Issue
from datetime import datetime, timedelta
from deepdiff import DeepDiff

"""
Para o devido entendimento desta DAG pode-se ter como base a seguinte explicação.

Esta DAG possui tarefas que são iniciadas a partir de um TRIGGER externo. As fases
de execução são às seguintes:

1) Ler a base TITLE em formato MST
1.1) Armazena output do isis2json na área de trabalho xcom

2) Ler a base ISSUE em formato MST
2.2) Armazena output do isis2json na área de trabalho xcom

3) Envia os dados da base TITLE para a API do Kernel
3.1) Itera entre os periódicos lidos da base TITLE
3.2) Converte o periódico para o formato JSON aceito pelo Kernel
3.3) Verifica se o Journal já existe na API Kernel
3.3.1) Faz o diff do entre o payload gerado e os metadados vindos do Kernel
3.3.2) Se houver diferenças faz-ze um PATCH para atualizar o registro
3.4) Se o Journal não existir
3.4.1) Remove as chaves nulas
3.4.2) Faz-se um PUT para criar o registro
3.5) Dispara o DAG subsequente.
"""

BASE_PATH = os.path.dirname(os.path.dirname(__file__))

JAVA_LIB_DIR = os.path.join(BASE_PATH, "utils/isis2json/lib/")

JAVA_LIBS_PATH = [
    os.path.join(JAVA_LIB_DIR, file)
    for file in os.listdir(JAVA_LIB_DIR)
    if file.endswith(".jar")
]

CLASSPATH = ":".join(JAVA_LIBS_PATH)

ISIS2JSON_PATH = os.path.join(BASE_PATH, "utils/isis2json/isis2json.py")

KERNEL_API_JOURNAL_ENDPOINT = "/journals/"
KERNEL_API_BUNDLES_ENDPOINT = "/bundles/"

default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "start_date": datetime(2019, 6, 25),
    "email": ["airflow@example.com"],
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

dag = DAG("kernel-gate", default_args=default_args, schedule_interval=None)


jython_command_template = """java -cp {{ params.classpath}} org.python.util.jython \
    {{ params.isis2json }} -t 3 -p 'v' --inline {{ params.file }}
"""

read_title_mst = BashOperator(
    task_id="read_title_mst",
    bash_command=jython_command_template,
    params={
        "file": Variable.get("BASE_TITLE"),
        "classpath": CLASSPATH,
        "isis2json": ISIS2JSON_PATH,
    },
    dag=dag,
    xcom_push=True,
)

read_issue_mst = BashOperator(
    task_id="read_issue_mst",
    bash_command=jython_command_template,
    params={
        "file": Variable.get("BASE_ISSUE"),
        "classpath": CLASSPATH,
        "isis2json": ISIS2JSON_PATH,
    },
    dag=dag,
    xcom_push=True,
)


def journal_as_kernel(journal: Journal) -> dict:
    """Gera um dicionário com a estrutura esperada pela API do Kernel a
    partir da estrutura gerada pelo isis2json"""

    _payload = {}
    _id = journal.any_issn()

    if not _id:
        _id = journal.scielo_issn

    _payload["_id"] = _id

    if journal.mission:
        _payload["mission"] = [
            {"language": lang, "value": value}
            for lang, value in journal.mission.items()
        ]
    else:
        _payload["mission"] = []

    _payload["title"] = journal.title or ""
    _payload["title_iso"] = journal.abbreviated_iso_title or ""
    _payload["short_title"] = journal.abbreviated_title or ""
    _payload["acronym"] = journal.acronym or ""
    _payload["scielo_issn"] = journal.scielo_issn or ""
    _payload["print_issn"] = journal.print_issn or ""
    _payload["electronic_issn"] = journal.electronic_issn or ""

    _payload["status"] = {}
    if journal.status_history:
        _status = journal.status_history[-1]
        _payload["status"]["status"] = _status[1]

        if _status[2]:
            _payload["status"]["reason"] = _status[2]

    _payload["subject_areas"] = []
    if journal.subject_areas:

        for subject_area in journal.subject_areas:
            # TODO: Algumas áreas estão em caixa baixa, o que devemos fazer?

            # A Base MST possui uma grande área que é considerada errada
            # é preciso normalizar o valor
            if subject_area.upper() == "LINGUISTICS, LETTERS AND ARTS":
                subject_area = "LINGUISTIC, LITERATURE AND ARTS"

            _payload["subject_areas"].append(subject_area.upper())

    _payload["sponsors"] = []
    if journal.sponsors:
        _payload["sponsors"] = [{"name": sponsor} for sponsor in journal.sponsors]

    _payload["subject_categories"] = journal.wos_subject_areas or []
    _payload["online_submission_url"] = journal.submission_url or ""

    _payload["next_journal"] = {}
    if journal.next_title:
        _payload["next_journal"]["name"] = journal.next_title

    _payload["previous_journal"] = {}
    if journal.previous_title:
        _payload["previous_journal"]["name"] = journal.previous_title

    _payload["contact"] = {}
    if journal.editor_email:
        _payload["contact"]["email"] = journal.editor_email

    if journal.editor_address:
        _payload["contact"]["address"] = journal.editor_address

    return _payload


def issue_id(issn_id, year, volume=None, number=None, supplement=None):
    """Reproduz ID gerado para os documents bundle utilizado na ferramenta
    de migração"""

    labels = ["issn_id", "year", "volume", "number", "supplement"]
    values = [issn_id, year, volume, number, supplement]

    data = dict([(label, value) for label, value in zip(labels, values)])

    labels = ["issn_id", "year"]
    _id = []
    for label in labels:
        value = data.get(label)
        if value:
            _id.append(value)

    labels = [("volume", "v"), ("number", "n"), ("supplement", "s")]
    for label, prefix in labels:
        value = data.get(label)
        if value:
            if value.isdigit():
                value = str(int(value))
            _id.append(prefix + value)

    return "-".join(_id)


def issue_as_kernel(issue: dict) -> dict:
    def parse_date(date: str) -> str:
        """Traduz datas em formato simples ano-mes-dia, ano-mes para
        o formato iso utilizado durantr a persistência do Kernel"""

        _date = None
        try:
            _date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            try:
                _date = datetime.strptime(date, "%Y-%m")
            except ValueError:
                _date = datetime.strptime(date, "%Y")

        return _date

    _payload = {}
    _payload["volume"] = issue.volume or ""
    _payload["number"] = issue.number or ""

    if issue.type is "supplement":
        _payload["supplement"] = (
            issue.supplement_volume or issue.supplement_number or "0"
        )

    if issue.titles:
        _titles = [
            {"language": lang, "value": value} for lang, value in issue.titles.items()
        ]
        _payload["titles"] = _titles
    else:
        _payload["titles"] = []

    if issue.start_month and issue.end_month:
        _publication_season = [int(issue.start_month), int(issue.end_month)]
        _payload["publication_season"] = sorted(set(_publication_season))
    else:
        _payload["publication_season"] = []

    issn_id = issue.data.get("issue").get("v35")[0]["_"]
    _creation_date = parse_date(issue.publication_date)

    _payload["_id"] = issue_id(
        issn_id,
        str(_creation_date.year),
        issue.volume,
        issue.number,
        _payload.get("supplement"),
    )

    return _payload


def register_or_update(_id: str, payload: dict, entity_url: str):
    """Cadastra ou atualiza uma entidade no Kernel a partir de um payload"""

    api_hook = HttpHook(http_conn_id="kernel_conn", method="GET")

    response = api_hook.run(
        endpoint="{}{}".format(entity_url, _id), extra_options={"check_response": False}
    )

    if response.status_code == http.client.NOT_FOUND:
        payload = {k: v for k, v in payload.items() if v}
        api_hook = HttpHook(http_conn_id="kernel_conn", method="PUT")
        response = api_hook.run(
            endpoint="{}{}".format(entity_url, _id),
            data=json.dumps(payload),
            extra_options={"check_response": False},
        )
    elif response.status_code == http.client.OK:
        _metadata = response.json()["metadata"]

        payload = {
            k: v
            for k, v in payload.items()
            if _metadata.get(k) or _metadata.get(k) == v or v
        }

        if DeepDiff(_metadata, payload, ignore_order=True):
            api_hook = HttpHook(http_conn_id="kernel_conn", method="PATCH")
            response = api_hook.run(
                endpoint="{}{}".format(entity_url, _id),
                data=json.dumps(payload),
                extra_options={"check_response": False},
            )

    return response


def process_journals(**context):
    """Processa uma lista de journals carregados a partir do resultado
    de leitura da base MST"""

    journals = context["ti"].xcom_pull(task_ids="read_title_mst")
    journals = json.loads(journals)
    journals_as_kernel = [journal_as_kernel(Journal(journal)) for journal in journals]

    for journal in journals_as_kernel:
        _id = journal.pop("_id")
        response = register_or_update(_id, journal, KERNEL_API_JOURNAL_ENDPOINT)


def process_issues(**context):
    """Processa uma lista de issues carregadas a partir do resultado
    de leitura da base MST"""

    def filter_issues(issues: List[Issue]) -> List[Issue]:
        """Filtra as issues em formato xylose sempre removendo
        os press releases e ahead of print"""

        filters = [
            lambda issue: not issue.type == "pressrelease",
            lambda issue: not issue.type == "ahead",
        ]

        for f in filters:
            issues = list(filter(f, issues))

        return issues

    issues = context["ti"].xcom_pull(task_ids="read_issue_mst")
    issues = json.loads(issues)
    issues = [Issue({"issue": data}) for data in issues]
    issues = filter_issues(issues)
    issues_as_kernel = [issue_as_kernel(issue) for issue in issues]

    for issue in issues_as_kernel:
        _id = issue.pop("_id")
        response = register_or_update(_id, issue, KERNEL_API_BUNDLES_ENDPOINT)


work_on_journals = PythonOperator(
    task_id="work_on_journals",
    python_callable=process_journals,
    dag=dag,
    provide_context=True,
    params={"process_journals": True},
)

work_on_issues = PythonOperator(
    task_id="work_on_issues",
    python_callable=process_issues,
    dag=dag,
    provide_context=True,
)

read_title_mst >> read_issue_mst
read_issue_mst >> work_on_journals
work_on_journals >> work_on_issues
