"""
Planificador de Visitas Tecnicas a Operaciones Mineras
=====================================================
Tarea personal - Programa Agentes IA (UTEC Posgrado) - LangGraph MultiAgents
Autor: Christian Monrroy Romani | JYC Automatica e Instrumentacion S.A.C.

Sistema multiagente orquestado en LangGraph que convierte una solicitud en
lenguaje natural ("necesito subir a Cuajone la proxima semana con Juan y Luis")
en un plan de visita tecnica completo: alcance por dia, logistica, requisitos
HSE y lista de materiales verificada contra stock.

Ejecuta 100% local sobre Ollama (Windows).
"""

from __future__ import annotations

import os
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

# =============================================================================
# 1. CONFIGURACION DEL MODELO LOCAL (Ollama)
# =============================================================================
# Requisitos previos en Windows:
#   winget install Ollama.Ollama
#   ollama pull qwen2.5:7b-instruct      (o qwen2.5:14b-instruct si hay >=12 GB VRAM)
#   ollama serve                          (normalmente ya corre como servicio)

MODELO = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
BASE_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# num_ctx es CRITICO: Ollama usa 2048 tokens por defecto y truncaria
# silenciosamente los prompts de consolidacion.
def make_llm(temperature: float = 0.2, num_ctx: int = 8192) -> ChatOllama:
    return ChatOllama(
        model=MODELO,
        base_url=BASE_URL,
        temperature=temperature,
        num_ctx=num_ctx,
    )


# =============================================================================
# 2. BASE DE CONOCIMIENTO OPERATIVA
# =============================================================================
# En produccion estas tres fuentes son: ficha de sitio (SharePoint),
# backlog de mantenimiento (Excel/CMMS) y stock (sistema contable).
# Aqui se simulan como diccionarios para que el notebook sea reproducible.

FICHAS_SITIO = {
    "Cuajone": {
        "cliente": "Southern Peru Copper Corporation - contrato SADAC",
        "region": "Moquegua",
        "altitud_m": 3500,
        "acceso": "Vuelo LIM-AQP (1h30) + camioneta Arequipa-Cuajone (4h30, 250 km)",
        "alojamiento": "Campamento Villa Cuajone. Reserva con 7 dias de anticipacion via SPCC.",
        "requisitos_ingreso": [
            "EMO de altura vigente (< 1 año)",
            "Induccion SST SPCC presencial (4 h) - solo martes y jueves 07:00",
            "SCTR salud y pension vigentes",
            "Fotocheck de contratista activo",
            "PETAR para trabajos en altura o energizados",
        ],
        "ventana_operativa": "Sistema de agua no admite parada > 2 h. Trabajos en linea requieren aval de Operaciones.",
        "jornada_efectiva_h": 8,
        "notas": "16 estaciones dispersas en 40 km. Traslado entre estaciones 30-60 min.",
    },
    "Toquepala": {
        "cliente": "Southern Peru Copper Corporation - Mina",
        "region": "Tacna",
        "altitud_m": 3500,
        "acceso": "Vuelo LIM-TCQ (1h50) + camioneta Tacna-Toquepala (3h, 130 km)",
        "alojamiento": "Campamento Toquepala. Reserva con 7 dias de anticipacion via SPCC.",
        "requisitos_ingreso": [
            "EMO de altura vigente (< 1 año)",
            "Induccion SST SPCC presencial (4 h)",
            "SCTR salud y pension vigentes",
            "Fotocheck de contratista activo",
            "Induccion especifica de Mina (operacion de equipo pesado)",
        ],
        "ventana_operativa": "Perforadoras se intervienen solo en cambio de guardia o parada programada.",
        "jornada_efectiva_h": 8,
        "notas": "Trabajo junto a equipo pesado en operacion: se requiere vigia permanente.",
    },
    "Ilo": {
        "cliente": "Southern Peru Copper Corporation - Refineria",
        "region": "Moquegua (nivel del mar)",
        "altitud_m": 25,
        "acceso": "Vuelo LIM-TCQ o LIM-AQP + camioneta a Ilo (3h)",
        "alojamiento": "Hotel en Ilo ciudad. Sin restriccion de reserva anticipada.",
        "requisitos_ingreso": [
            "EMO basico vigente",
            "Induccion SST SPCC presencial (4 h)",
            "SCTR salud y pension vigentes",
            "Fotocheck de contratista activo",
        ],
        "ventana_operativa": "Sala electrica accesible en horario administrativo.",
        "jornada_efectiva_h": 9,
        "notas": "Sin restriccion de altura. No requiere aclimatacion.",
    },
}

BACKLOG = {
    "Cuajone": [
        {"id": "CJ-001", "estacion": "Bocatoma Torata", "sistema": "ControlLogix 1756-L71",
         "descripcion": "Bateria de CPU agotada, riesgo de perdida de programa",
         "prioridad": 1, "hh": 3, "repuestos": ["1756-BA2"]},
        {"id": "CJ-002", "estacion": "PS-07 Chuntacala", "sistema": "1756-EN2TR",
         "descripcion": "Puerto 2 sin enlace, anillo DLR abierto (sin redundancia)",
         "prioridad": 1, "hh": 5, "repuestos": ["1756-EN2TR", "patchcord Cat6 industrial"]},
        {"id": "CJ-003", "estacion": "Rebombeo R-2", "sistema": "CompactLogix 1769-L33ER",
         "descripcion": "Comunicacion EtherNet/IP intermitente con VFD PowerFlex 525",
         "prioridad": 1, "hh": 6, "repuestos": ["patchcord Cat6 industrial"]},
        {"id": "CJ-004", "estacion": "Planta de Filtros", "sistema": "PanelView Plus 7",
         "descripcion": "HMI sin retroiluminacion, operador trabaja a ciegas",
         "prioridad": 2, "hh": 4, "repuestos": ["2711P-T10C22D9P"]},
        {"id": "CJ-005", "estacion": "Reservorio R-4", "sistema": "Transmisor de nivel radar",
         "descripcion": "Deriva de 12 cm respecto a regla graduada",
         "prioridad": 2, "hh": 3, "repuestos": ["calibrador HART 475"]},
        {"id": "CJ-006", "estacion": "Sala de Control", "sistema": "FactoryTalk View SE",
         "descripcion": "Backup de aplicacion desactualizado y tags huerfanos",
         "prioridad": 3, "hh": 8, "repuestos": []},
    ],
    "Toquepala": [
        {"id": "TQ-001", "estacion": "Perforadora P&H 320", "sistema": "Control remoto",
         "descripcion": "Perdida de enlace de radio por encima de 150 m",
         "prioridad": 1, "hh": 8, "repuestos": ["antena omni 900 MHz", "cable LMR-400"]},
        {"id": "TQ-002", "estacion": "Perforadora P&H 100", "sistema": "Centurion",
         "descripcion": "Migracion de firmware pendiente de reimplementacion",
         "prioridad": 2, "hh": 12, "repuestos": ["laptop con Centurion Toolkit"]},
        {"id": "TQ-003", "estacion": "Taller Mina", "sistema": "Banco de pruebas",
         "descripcion": "Verificacion de modulos de repuesto en almacen de mina",
         "prioridad": 3, "hh": 4, "repuestos": []},
    ],
    "Ilo": [
        {"id": "IL-001", "estacion": "Sala Electrica RI", "sistema": "Servidor Dell Precision 7960",
         "descripcion": "Eventos de PSU registrados en SEL de iDRAC",
         "prioridad": 1, "hh": 4, "repuestos": ["fuente redundante 1400W"]},
        {"id": "IL-002", "estacion": "MDC", "sistema": "ThinManager",
         "descripcion": "Terminal MDC-TC-01 sin failover configurado",
         "prioridad": 2, "hh": 3, "repuestos": []},
        {"id": "IL-003", "estacion": "Panel TC-J&C-RI-IE-1000", "sistema": "ControlLogix",
         "descripcion": "Pruebas FAT pendientes de 4 lazos de control",
         "prioridad": 2, "hh": 10, "repuestos": []},
    ],
}

STOCK_LIMA = {
    "1756-BA2": 4,
    "1756-EN2TR": 1,
    "patchcord Cat6 industrial": 12,
    "2711P-T10C22D9P": 0,
    "calibrador HART 475": 1,
    "antena omni 900 MHz": 0,
    "cable LMR-400": 1,
    "fuente redundante 1400W": 0,
    "laptop con Centurion Toolkit": 2,
}

LEAD_TIME_DIAS = {
    "2711P-T10C22D9P": 21,
    "antena omni 900 MHz": 10,
    "fuente redundante 1400W": 30,
}


# =============================================================================
# 3. HERRAMIENTAS (Agent-Computer Interface)
# =============================================================================
# Docstrings redactados como contrato para el modelo: que devuelve, con que
# valores exactos se invoca y cual es el caso borde. (Anthropic, Apendice 2:
# "prompt engineering your tools").

@tool
def ficha_sitio(sitio: str) -> str:
    """Devuelve la ficha operativa de una operacion minera: altitud, ruta de
    acceso desde Lima, alojamiento, requisitos de ingreso obligatorios,
    ventana operativa y jornada efectiva permitida.

    El argumento 'sitio' debe ser exactamente uno de: "Cuajone", "Toquepala", "Ilo".
    Si el sitio no existe devuelve un mensaje de error indicando las opciones validas.
    """
    ficha = FICHAS_SITIO.get(sitio)
    if not ficha:
        return f"ERROR: sitio '{sitio}' no registrado. Opciones validas: {list(FICHAS_SITIO)}"
    return json.dumps(ficha, ensure_ascii=False, indent=2)


@tool
def pendientes_sitio(sitio: str) -> str:
    """Devuelve el backlog de mantenimiento abierto del sitio: id del pendiente,
    estacion, sistema afectado, descripcion de la falla, prioridad
    (1 = critica, 2 = media, 3 = baja), horas-hombre estimadas y repuestos requeridos.

    El argumento 'sitio' debe ser exactamente uno de: "Cuajone", "Toquepala", "Ilo".
    Devuelve lista vacia si el sitio no tiene pendientes abiertos.
    """
    items = BACKLOG.get(sitio, [])
    return json.dumps(items, ensure_ascii=False, indent=2)


@tool
def stock_almacen(item: str) -> str:
    """Consulta la disponibilidad de UN repuesto en el almacen de JYC en Lima.

    El argumento 'item' debe ser el nombre exacto del repuesto tal como aparece
    en el campo 'repuestos' del backlog (por ejemplo "1756-BA2").
    Devuelve la cantidad disponible y, si la cantidad es 0, el lead time de
    compra en dias calendario.
    """
    cant = STOCK_LIMA.get(item)
    if cant is None:
        return f"{item}: no catalogado en almacen Lima. Requiere cotizacion (lead time no estimado)."
    if cant == 0:
        lt = LEAD_TIME_DIAS.get(item, 15)
        return f"{item}: SIN STOCK. Lead time de compra: {lt} dias calendario."
    return f"{item}: {cant} unidad(es) disponibles en almacen Lima."


HERRAMIENTAS = [ficha_sitio, pendientes_sitio, stock_almacen]


# =============================================================================
# 4. ESQUEMAS ESTRUCTURADOS
# =============================================================================

class Contexto(BaseModel):
    """Datos extraidos de la solicitud en lenguaje natural."""
    sitio: str = Field(description='Uno de: "Cuajone", "Toquepala", "Ilo", o "" si no se menciona')
    fecha_inicio: str = Field(description='Fecha de inicio en formato YYYY-MM-DD, o "" si no se menciona')
    dias: int = Field(default=0, description="Duracion en dias de campo; 0 si no se menciona")
    personal: list[str] = Field(default_factory=list, description="Nombres del personal que viaja")
    objetivos: list[str] = Field(default_factory=list, description="Objetivos explicitos mencionados por el usuario")


class Critica(BaseModel):
    """Resultado del evaluador del plan."""
    veredicto: Literal["APROBADO", "OBSERVADO"]
    observaciones: list[str] = Field(default_factory=list, description="Que corregir; vacio si APROBADO")


class EstadoVisita(TypedDict, total=False):
    """Estado compartido que se propaga por el grafo."""
    solicitud: str
    ctx: dict
    faltantes: list[str]
    datos: dict
    s_tecnico: str
    s_logistica: str
    s_hseq: str
    s_materiales: str
    plan: str
    critica: str
    veredicto: str
    iteracion: int
    entregable: str


# =============================================================================
# 5. NODOS DEL GRAFO
# =============================================================================

def n_interpretar(state: EstadoVisita) -> dict:
    """Extrae datos estructurados de la solicitud y aplica un gate deterministico."""
    llm = make_llm(temperature=0).with_structured_output(Contexto)
    hoy = date.today().isoformat()
    ctx: Contexto = llm.invoke([
        SystemMessage(content=(
            "Eres un asistente de planificacion de JYC Automatica (Peru). Extrae los datos "
            f"de la solicitud. Hoy es {hoy}. Resuelve fechas relativas ('la proxima semana', "
            "'el lunes') a formato YYYY-MM-DD. Sitios validos: Cuajone, Toquepala, Ilo. "
            "No inventes datos que no esten en el texto: usa cadena vacia o 0."
        )),
        HumanMessage(content=state["solicitud"]),
    ])

    # Gate deterministico: la validacion NO se delega al LLM.
    faltantes = []
    if ctx.sitio not in FICHAS_SITIO:
        faltantes.append("sitio (Cuajone / Toquepala / Ilo)")
    try:
        datetime.strptime(ctx.fecha_inicio, "%Y-%m-%d")
    except (ValueError, TypeError):
        faltantes.append("fecha de inicio (YYYY-MM-DD)")
    if ctx.dias < 1:
        faltantes.append("duracion en dias de campo")
    if not ctx.personal:
        faltantes.append("personal que viaja")

    return {"ctx": ctx.model_dump(), "faltantes": faltantes, "iteracion": 0}


def r_gate(state: EstadoVisita) -> Literal["pedir_datos", "recolectar"]:
    """Arista condicional: si falta informacion critica, se detiene y pregunta."""
    return "pedir_datos" if state["faltantes"] else "recolectar"


def n_pedir_datos(state: EstadoVisita) -> dict:
    """Human-in-the-loop: pausa la ejecucion y devuelve el control al usuario."""
    respuesta = interrupt({
        "mensaje": "Faltan datos para planificar la visita.",
        "faltantes": state["faltantes"],
        "entendido_hasta_ahora": state["ctx"],
    })
    return {"solicitud": state["solicitud"] + "\nInformacion adicional: " + str(respuesta)}


def n_recolectar(state: EstadoVisita) -> dict:
    """Invocacion determinista de herramientas: los mismos datos para los 4 workers."""
    sitio = state["ctx"]["sitio"]
    ficha = json.loads(ficha_sitio.invoke({"sitio": sitio}))
    pend = json.loads(pendientes_sitio.invoke({"sitio": sitio}))

    requeridos = sorted({r for p in pend for r in p["repuestos"]})
    stock = {r: stock_almacen.invoke({"item": r}) for r in requeridos}

    return {"datos": {"ficha": ficha, "pendientes": pend, "stock": stock}}


def _worker(rol: str, instruccion: str, state: EstadoVisita, temperature: float = 0.3) -> str:
    """Fabrica de workers: mismo contexto, prompt especializado por rol."""
    llm = make_llm(temperature=temperature)
    ctx = state["ctx"]
    d = state["datos"]
    msg = llm.invoke([
        SystemMessage(content=(
            f"Eres el {rol} de JYC Automatica e Instrumentacion (Lima, Peru), empresa "
            "integradora de automatizacion industrial. Respondes en español tecnico, "
            "conciso y accionable. Nunca inventas datos que no esten en el contexto. "
            "Formato: viñetas markdown, maximo 12 lineas. Sin introduccion ni cierre."
        )),
        HumanMessage(content=(
            f"SOLICITUD: {ctx}\n\n"
            f"FICHA DEL SITIO:\n{json.dumps(d['ficha'], ensure_ascii=False)}\n\n"
            f"BACKLOG ABIERTO:\n{json.dumps(d['pendientes'], ensure_ascii=False)}\n\n"
            f"STOCK ALMACEN LIMA:\n{json.dumps(d['stock'], ensure_ascii=False)}\n\n"
            f"TU TAREA: {instruccion}"
        )),
    ])
    return msg.content


def n_tecnico(state: EstadoVisita) -> dict:
    return {"s_tecnico": _worker(
        "Ingeniero de Planificacion Tecnica",
        "Selecciona que pendientes se atienden en esta visita y en que orden. "
        "Respeta la jornada efectiva del sitio y la duracion solicitada. "
        "Prioridad 1 es obligatoria. Para cada pendiente indica: id, estacion, "
        "horas-hombre y a quien del equipo se asigna. Si el backlog no cabe en los "
        "dias disponibles, di explicitamente que queda diferido.",
        state)}


def n_logistica(state: EstadoVisita) -> dict:
    return {"s_logistica": _worker(
        "Coordinador de Logistica y Viajes",
        "Arma el itinerario puerta a puerta: vuelos, traslado terrestre, alojamiento "
        "y traslados internos entre estaciones. Considera que el dia de viaje NO es "
        "dia productivo completo. Indica que reservas deben hacerse y con cuanta "
        "anticipacion.",
        state)}


def n_hseq(state: EstadoVisita) -> dict:
    return {"s_hseq": _worker(
        "Supervisor de Seguridad y Salud Ocupacional (SSOMA)",
        "Lista los requisitos habilitantes de ingreso para CADA persona del equipo, "
        "con responsable y fecha limite relativa al inicio del viaje. Evalua el riesgo "
        "de altura geografica y define si corresponde aclimatacion. Indica que permisos "
        "de trabajo (PETAR) se requieren segun los pendientes seleccionados.",
        state)}


def n_materiales(state: EstadoVisita) -> dict:
    return {"s_materiales": _worker(
        "Jefe de Almacen y Repuestos",
        "Consolida la lista de materiales, herramientas e instrumentos a llevar. "
        "Marca EXPLICITAMENTE cada item sin stock y propone accion: compra urgente, "
        "prestamo del almacen de mina, o diferir el pendiente asociado. "
        "Recuerda que una vez en sitio no hay reabastecimiento rapido.",
        state)}


def n_consolidar(state: EstadoVisita) -> dict:
    """Sintetiza las 4 secciones en un plan dia por dia."""
    llm = make_llm(temperature=0.2, num_ctx=16384)
    correccion = ""
    if state.get("critica"):
        correccion = (
            "\n\nEl plan anterior fue OBSERVADO. Corrige exactamente estos puntos:\n"
            + state["critica"] + "\n\nPLAN ANTERIOR:\n" + state.get("plan", "")
        )
    msg = llm.invoke([
        SystemMessage(content=(
            "Eres el Gerente de Proyectos de JYC Automatica. Integras los aportes de tu "
            "equipo en UN plan de visita coherente en español. Estructura obligatoria:\n"
            "## Resumen ejecutivo (3 lineas)\n"
            "## Programa dia por dia (tabla markdown: Dia | Fecha | Actividad | Responsable | HH)\n"
            "## Riesgos y contingencias (maximo 4)\n"
            "No repitas texto de las secciones fuente; integralo."
        )),
        HumanMessage(content=(
            f"CONTEXTO: {state['ctx']}\n\n"
            f"### Plan tecnico\n{state['s_tecnico']}\n\n"
            f"### Logistica\n{state['s_logistica']}\n\n"
            f"### SSOMA\n{state['s_hseq']}\n\n"
            f"### Materiales\n{state['s_materiales']}"
            + correccion
        )),
    ])
    return {"plan": msg.content}


RUBRICA = """1. Todo pendiente de prioridad 1 esta programado o su diferimiento esta justificado.
2. Ningun dia excede la jornada efectiva del sitio (horas-hombre coherentes con el equipo).
3. Si la altitud supera 3000 m, el primer dia tiene carga reducida por aclimatacion.
4. Cada repuesto SIN STOCK tiene una accion definida (compra, prestamo o diferimiento).
5. Los requisitos de ingreso SSOMA aparecen con responsable y fecha limite.
6. Los dias de viaje no se contabilizan como dias productivos completos."""


def n_evaluar(state: EstadoVisita) -> dict:
    """Evaluator-optimizer: critica el plan contra una rubrica fija."""
    llm = make_llm(temperature=0).with_structured_output(Critica)
    c: Critica = llm.invoke([
        SystemMessage(content=(
            "Eres auditor de planificacion. Evalua el plan contra la rubrica. "
            "Marca OBSERVADO solo si incumple un criterio de forma concreta y verificable. "
            "Cada observacion debe ser una instruccion de correccion, no una queja."
        )),
        HumanMessage(content=f"RUBRICA:\n{RUBRICA}\n\nPLAN:\n{state['plan']}"),
    ])
    return {
        "veredicto": c.veredicto,
        "critica": "\n".join(f"- {o}" for o in c.observaciones),
        "iteracion": state.get("iteracion", 0) + 1,
    }


MAX_ITER = 2


def r_evaluacion(state: EstadoVisita) -> Literal["consolidar", "entregable"]:
    """Lazo de refinamiento con tope duro de iteraciones."""
    if state["veredicto"] == "OBSERVADO" and state["iteracion"] < MAX_ITER:
        return "consolidar"
    return "entregable"


def n_entregable(state: EstadoVisita) -> dict:
    """Nodo puramente deterministico: formatea, no razona."""
    ctx = state["ctx"]
    d = state["datos"]
    sin_stock = [k for k, v in d["stock"].items() if "SIN STOCK" in v or "no catalogado" in v]
    p1 = [p["id"] for p in d["pendientes"] if p["prioridad"] == 1]

    doc = f"""# Plan de Visita Tecnica - {ctx['sitio']}
**Cliente:** {d['ficha']['cliente']}
**Inicio:** {ctx['fecha_inicio']} | **Duracion:** {ctx['dias']} dias
**Equipo:** {', '.join(ctx['personal'])}
**Altitud:** {d['ficha']['altitud_m']} m.s.n.m.
**Generado:** {datetime.now():%Y-%m-%d %H:%M} | Iteraciones de refinamiento: {state['iteracion']} | Veredicto: {state['veredicto']}

---

{state['plan']}

---

## Anexo A - Alcance tecnico
{state['s_tecnico']}

## Anexo B - Logistica
{state['s_logistica']}

## Anexo C - SSOMA
{state['s_hseq']}

## Anexo D - Materiales
{state['s_materiales']}

---

## Alertas automaticas (verificacion deterministica)
- Pendientes de prioridad 1 en el sitio: {', '.join(p1) if p1 else 'ninguno'}
- Items sin stock en Lima: {', '.join(sin_stock) if sin_stock else 'ninguno'}
- Requiere aclimatacion: {'SI' if d['ficha']['altitud_m'] > 3000 else 'NO'}

> Documento generado por un sistema asistido por IA. Requiere revision y firma
> del Gerente General antes de su ejecucion.
"""
    return {"entregable": doc}


# =============================================================================
# 6. CONSTRUCCION DEL GRAFO
# =============================================================================

def construir_grafo(con_checkpointer: bool = True):
    g = StateGraph(EstadoVisita)

    g.add_node("interpretar", n_interpretar)
    g.add_node("pedir_datos", n_pedir_datos)
    g.add_node("recolectar", n_recolectar)
    g.add_node("tecnico", n_tecnico)
    g.add_node("logistica", n_logistica)
    g.add_node("hseq", n_hseq)
    g.add_node("materiales", n_materiales)
    g.add_node("consolidar", n_consolidar)
    g.add_node("evaluar", n_evaluar)
    g.add_node("entregable", n_entregable)

    g.add_edge(START, "interpretar")
    g.add_conditional_edges("interpretar", r_gate, ["pedir_datos", "recolectar"])
    g.add_edge("pedir_datos", "interpretar")

    # Fan-out: los 4 workers corren en paralelo (mismo superstep)
    for w in ("tecnico", "logistica", "hseq", "materiales"):
        g.add_edge("recolectar", w)
        g.add_edge(w, "consolidar")   # fan-in

    g.add_edge("consolidar", "evaluar")
    g.add_conditional_edges("evaluar", r_evaluacion, ["consolidar", "entregable"])
    g.add_edge("entregable", END)

    return g.compile(checkpointer=MemorySaver() if con_checkpointer else None)


# =============================================================================
# 7. EJECUCION: avance en vivo + registro de la corrida
# =============================================================================
# app.stream(..., stream_mode="updates") entrega una actualizacion por cada nodo
# que termina, en el momento en que termina -- incluidos los 4 workers en
# paralelo, que llegan intercalados segun terminan, no en el orden en que se
# lanzaron. Es lo que permite mostrar avance real en vez de esperar a ciegas.

SOLICITUD_DEMO = (
    "Necesito subir a Cuajone con Juan y Luis para atender los pendientes "
    "criticos. Fecha de inicio 2026-08-25, 3 dias de campo."
)

# Respuestas usadas para reanudar automaticamente cuando no hay una consola
# interactiva detras (por ejemplo, una corrida disparada desde otro proceso).
# Con una consola real, _responder_interrupcion pregunta en vivo y estas
# nunca se usan; si se usan, el registro de la corrida lo deja explicito.
_RESPUESTAS_DEMO = {
    "sitio (Cuajone / Toquepala / Ilo)": "Cuajone",
    "fecha de inicio (YYYY-MM-DD)": "2026-08-25",
    "duracion en dias de campo": "3 dias",
    "personal que viaja": "Juan, Luis",
}


def _elapsed(t0: float) -> str:
    s = int(time.time() - t0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _responder_interrupcion(payload: dict) -> str:
    """Resuelve un punto de interrupcion (human-in-the-loop): pregunta en
    consola los datos que el gate marco como faltantes. Sin consola
    interactiva usa la respuesta de demostracion, y lo dice explicitamente
    para que quede claro en el registro que nadie humano respondio.
    """
    print(f"\n[PAUSA] {payload['mensaje']}")
    for f in payload["faltantes"]:
        print(f"         - {f}")

    if not sys.stdin.isatty():
        print("         (sin consola interactiva: se usa una respuesta de demostracion)")
        partes = [f"{f}: {_RESPUESTAS_DEMO.get(f, 'no especificado')}" for f in payload["faltantes"]]
        return ". ".join(partes)

    partes = []
    for etiqueta in payload["faltantes"]:
        valor = input(f"    {etiqueta}: ").strip()
        partes.append(f"{etiqueta}: {valor or _RESPUESTAS_DEMO.get(etiqueta, 'no especificado')}")
    return ". ".join(partes)


def _resumir_actualizacion(nodo: str, valor: dict) -> str:
    """Una linea legible por nodo, para la vista de avance en vivo."""
    if nodo == "interpretar":
        faltan = valor.get("faltantes") or []
        return "datos completos" if not faltan else f"faltan datos: {', '.join(faltan)}"
    if nodo == "recolectar":
        d = valor["datos"]
        sin_stock = sum(1 for v in d["stock"].values() if "SIN STOCK" in v or "no catalogado" in v)
        return f"{len(d['pendientes'])} pendiente(s) en backlog, {sin_stock} repuesto(s) sin stock"
    if nodo == "evaluar":
        n_obs = len(valor["critica"].splitlines()) if valor.get("critica") else 0
        return f"{valor['veredicto']} ({n_obs} observacion(es))"
    if nodo == "entregable":
        return f"plan final listo ({len(valor['entregable'])} caracteres)"
    return "completado"


def guardar_corrida(solicitud: str, registro: list[str], estado: dict) -> Path:
    """Guarda la corrida completa como markdown: solicitud, avance nodo por
    nodo con marca de tiempo, y el entregable final. Misma logica que
    cotizaciones/ en la tarea 01: cada corrida queda trazable sin depender
    de que alguien haya visto la consola en el momento.
    """
    carpeta = Path(__file__).parent / "corridas"
    carpeta.mkdir(exist_ok=True)
    sitio = (estado.get("ctx") or {}).get("sitio", "sin-sitio").lower() or "sin-sitio"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = carpeta / f"{ts}_{sitio}.md"

    doc = (
        f"# Corrida — Planificador de Visitas Tecnicas\n\n"
        f"- **Fecha:** {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"- **Modelo:** `{MODELO}` (Ollama local)\n"
        f"- **Veredicto:** {estado.get('veredicto', '?')} "
        f"(iteraciones de refinamiento: {estado.get('iteracion', 0)})\n\n"
        f"## Solicitud\n\n{solicitud}\n\n"
        f"## Registro de ejecucion\n\n```\n" + "\n".join(registro) + "\n```\n\n"
        f"## Entregable\n\n{estado.get('entregable', '(no se genero)')}\n"
    )
    ruta.write_text(doc, encoding="utf-8")
    return ruta


def main() -> None:
    try:  # consola de Windows: evitar mojibake en acentos
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    solicitud = " ".join(sys.argv[1:]).strip() or SOLICITUD_DEMO

    print("=" * 78)
    print("PLANIFICADOR DE VISITAS TECNICAS — JYC AUTOMATICA E INSTRUMENTACION".center(78))
    print(f"Modelo: {MODELO} (Ollama local)".center(78))
    print("=" * 78)
    print(f"\n>>> SOLICITUD\n{solicitud}\n")

    app = construir_grafo()
    config = {"configurable": {"thread_id": f"demo-{int(time.time())}"}}
    t0 = time.time()

    registro: list[str] = [f">>> SOLICITUD\n{solicitud}"]
    estado: dict = {}
    entrada: dict | Command = {"solicitud": solicitud}
    pendiente_de_reanudar = True

    while pendiente_de_reanudar:
        pendiente_de_reanudar = False
        for chunk in app.stream(entrada, config=config, stream_mode="updates"):
            for nodo, valor in chunk.items():
                if nodo == "__interrupt__":
                    payload = valor[0].value
                    respuesta = _responder_interrupcion(payload)
                    print(f"[{_elapsed(t0)}] Respuesta registrada: {respuesta}\n")
                    registro.append(
                        f"[{_elapsed(t0)}] PAUSA -> faltan: {', '.join(payload['faltantes'])}"
                    )
                    registro.append(f"Respuesta: {respuesta}")
                    entrada = Command(resume=respuesta)
                    pendiente_de_reanudar = True
                    continue
                linea = f"[{_elapsed(t0)}] {nodo:<12} {_resumir_actualizacion(nodo, valor)}"
                print(linea)
                registro.append(linea)
                estado.update(valor)

    print(f"\n{'=' * 78}")
    print(f"VEREDICTO: {estado.get('veredicto')} | tiempo total: {_elapsed(t0)}")
    print("=" * 78)
    print(estado.get("entregable", "[SISTEMA] No se genero entregable.\n"))

    ruta = guardar_corrida(solicitud, registro, estado)
    print(f"[ARCHIVO] Corrida guardada en: {ruta}\n")


if __name__ == "__main__":
    main()