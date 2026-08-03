# Agente Cotizador de Tableros Eléctricos Rockwell

Agente **ReAct** que convierte un requerimiento de ingeniería en lenguaje natural
("14 motores de 15 HP, 20 señales 4-20 mA, EtherNet/IP") en una configuración de
tablero de control Rockwell dimensionada y cotizada.

**Autor:** Christian Monrroy — JYC Automática e Instrumentación S.A.C.
**Curso:** Programa de Agentes IA, UTEC Posgrado (MEng. Boris Alzamora)

> ⚠️ **Los datos del catálogo son ficticios.** Los códigos Rockwell son reales, pero
> los precios, consumos de backplane, stock y proyectos del histórico fueron
> generados para este ejercicio académico. No corresponden a listas de precios de
> Rockwell Automation ni a información comercial de JYC.

---

## Agente, no workflow

Según [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
(Anthropic, 2024), un *workflow* recorre rutas de código predeterminadas mientras que
un *agente* dirige dinámicamente su propio proceso. Este proyecto está construido
sobre esa distinción:

| Componente | Naturaleza |
|---|---|
| Selección de plataforma (CompactLogix vs ControlLogix) | Agéntico |
| Dimensionamiento del I/O y conteo de módulos | Agéntico |
| Detección de obsolescencia y quiebre de stock, y su corrección | Agéntico |
| Cálculo de IGV (18%) y formateo de la tabla | **Determinista a propósito** |

El system prompt ([`prompt_sistema.md`](prompt_sistema.md)) define **objetivo y
restricciones**, nunca una secuencia de pasos. Ninguna restricción indica qué
herramienta usar ni en qué orden: eso lo decide el modelo.

## Herramientas (8)

| Herramienta | Función |
|---|---|
| `buscar_catalogo` | Busca productos por texto libre |
| `detalle_producto` | Ficha técnica: canales, consumo, slots, capacidad |
| `verificar_stock` | Disponibilidad y vigencia comercial |
| `buscar_reemplazo` | Sucesores para descontinuados o sin stock |
| `dimensionar_fuente` | Consumo de backplane contra capacidad de la fuente |
| `verificar_slots_chasis` | Ocupación de chasis 1756 o expansión local 5069 |
| `calcular` | Aritmética (evaluador AST, sin `eval`) |
| `consultar_historico` | Proyectos previos como referencia de alcance y precio |

## Lazos de corrección

El catálogo incluye trampas deliberadas que obligan al agente a replantear:

- **`1756-PA72`** está descontinuada → debe encontrar la sucesora `1756-PA75`.
- **`1756-IF8`** tiene stock 0 con lead time de 22 semanas → debe sustituirlo por
  `1756-IF8I`, que cuesta más y consume distinto, invalidando el dimensionamiento
  de fuente y la ocupación de chasis que ya hubiera calculado.

Además, `auditar_propuesta()` implementa el patrón **evaluator-optimizer**: revisa la
propuesta final contra las restricciones y devuelve el control al agente enumerando
qué incumplió, sin decirle cómo arreglarlo.

## Uso

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

ollama pull llama3.2
python agente.py
python agente.py "12 motores 10HP, 8 señales 4-20mA, EtherNet/IP"
```

Cada corrida se guarda en [`cotizaciones/`](cotizaciones/) como un markdown con el
requerimiento, la traza ReAct completa, la auditoría y la tabla con IGV.

El modelo es configurable sin tocar el código:

```powershell
$env:MODELO_OLLAMA = "qwen2.5:7b"; python agente.py
```

## Hallazgo: la plantilla de llama3.2 en Ollama rompe el lazo ReAct

Durante las pruebas el agente ignoraba por completo los resultados de sus
herramientas y respondía *"no tengo acceso a información en tiempo real"*. La causa
no era el modelo sino la plantilla de chat de Ollama:

```
{{- if eq .Role "user" }}<|start_header_id|>user<|end_header_id|>
{{- if and $.Tools $last }}      ← las tools solo se inyectan si el user es el ÚLTIMO mensaje
```

En un lazo ReAct, después de actuar el último mensaje es de rol `tool`. Desde el
segundo turno el modelo deja de ver que tiene herramientas y recibe un mensaje
`ipython` huérfano; sumado a la línea `Cutting Knowledge Date: December 2023` que la
plantilla inyecta siempre, responde desde su prior de entrenamiento.

Verificado de forma aislada: el mismo dato entregado como `HumanMessage` se usa
correctamente; entregado como `ToolMessage`, se ignora.

**Solución:** `_observaciones_como_usuario()`, un `pre_model_hook` que reexpresa las
observaciones como mensajes de usuario. Solo altera lo que ve el LLM
(`llm_input_messages`); el estado conserva los `ToolMessage` reales, que son los que
leen el auditor y la capa determinista.

## Limitación conocida

Con `llama3.2` (3B) el lazo agéntico funciona y el modelo se recupera de sus propios
errores de argumentos, pero no sostiene la planificación de extremo a extremo:
divaga entre validaciones y no converge a una propuesta final. Para completar el
ciclo se requiere un modelo con mejor uso de herramientas (~7B).

## Stack

Python 3.12 · LangChain 1.3 · LangGraph 1.2 (`create_react_agent`) · Ollama
