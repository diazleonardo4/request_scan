# Cómo Funciona el Servicio Request Scan

## Descripción General

El Servicio Request Scan es una API web que automatiza la obtención de datos de solicitudes de generación solar desde los portales de las empresas de servicios públicos colombianas **Afinia** (`servicios.energiacaribemar.co`) y **Air-e** (`servicios.air-e.com`). Dado que estos portales son aplicaciones web estándar no diseñadas para acceso programático, el servicio simula una sesión de navegador para navegar por sus formularios y extraer la información.

Todos los trabajos se ejecutan en segundo plano. El servicio responde de inmediato con un `job_id` y luego envía los resultados de forma incremental a una **URL de webhook** que usted provee (en este caso, una aplicación web de Google Apps Script que escribe en Google Sheets).

---

## Los Cuatro Endpoints

| Endpoint | Propósito | Caso de uso |
|---|---|---|
| `POST /scan/range` | Escanea un rango numérico de IDs para descubrir cuáles existen | Descubrimiento diario de nuevas solicitudes |
| `POST /audit/batch` | Obtiene el historial de auditoría para una lista de IDs conocidos | Recuperar el historial de registros existentes |
| `POST /status/refresh` | Verifica cambios de estado en IDs monitoreados | Seguimiento diario de solicitudes activas |
| `POST /fetch/batch` | Re-obtiene los datos completos del formulario para una lista de IDs conocidos | Corrección de registros tras problemas de datos |

También existe un endpoint `GET /health` disponible para pings de mantenimiento de conexión.

---

## Cómo Se Recuperan los Datos (El Flujo de Sesión)

Los portales de las empresas funcionan sobre **ASP.NET WebForms**, que almacena el estado del lado del servidor en una sesión vinculada a una cookie del navegador. Para recuperar los datos de cualquier ID de solicitud, el servicio debe replicar exactamente lo que haría un navegador — en cuatro pasos, todos dentro de la misma sesión:

```
1. ValidaSolicitud        → Confirma que el ID existe
2. Encryptar              → Obtiene un token cifrado y firmado por el servidor para ese ID
3. GET WFSolicitud.aspx   → Carga la página del formulario con el token, inicializando la sesión del servidor
4. CargarDatosSolicitud   → Llama al método de la página que retorna los datos
```

Restricción crítica: **los cuatro pasos deben usar la misma sesión HTTP** (la misma cookie). Si se dividen entre sesiones distintas, el estado del servidor establecido en el paso 3 no está disponible cuando se ejecuta el paso 4, y el servidor devuelve datos vacíos o incorrectos. El servicio crea una **sesión nueva por cada ID** para garantizar este aislamiento.

---

## El Sistema de Eventos Webhook

Cada endpoint recibe un `webhook_url`. A medida que el trabajo en segundo plano avanza, envía eventos JSON estructurados a esa URL:

| Evento | Cuándo se dispara |
|---|---|
| `scan_started` / `audit_started` | El trabajo comienza |
| `item` / `audit_item` | Un ID válido fue procesado exitosamente |
| `item_error` | Un ID falló |
| `status_change` | El estado de un ID monitoreado es diferente al último registrado |
| `scan_finished` / `audit_finished` | El trabajo terminó con un resumen de estadísticas |

Cada evento incluye un `job_id` para que el receptor pueda correlacionar eventos de un trabajo de larga duración.

---

## Estrategias de Escaneo (`/scan/range`)

El endpoint soporta dos estrategias de recorrido:

- **`checkpoint`** (predeterminada): Solo sondea IDs cuyos dos últimos dígitos están en `{10, 30, 50, 70, 90}`. Cuando encuentra un ID válido, expande secuencialmente hasta encontrar el primer ID inválido, y luego salta al siguiente checkpoint. Mucho más rápida para rangos dispersos.
- **`linear`**: Prueba cada ID del rango uno por uno. Usar cuando el rango es denso o cuando se requiere cobertura garantizada.

---

## Restricción de Concurrencia

Los servidores de las empresas tienen un error conocido de concurrencia a nivel de IP: cuando dos o más solicitudes del mismo origen llegan simultáneamente, el servidor puede contaminar el estado en memoria de las sesiones y devolver datos del ID incorrecto. Por esta razón, **solo debe ejecutarse un trabajo a la vez** contra el mismo operador. Ejecutar múltiples llamadas a `/scan/range` o `/fetch/batch` en paralelo producirá datos poco confiables.

---

## Operadores

| Clave del operador | Empresa | Portal |
|---|---|---|
| `"afinia"` | Energía Caribemar (Afinia) | `servicios.energiacaribemar.co` |
| `"aire"` | Air-e | `servicios.air-e.com` |

Air-e requiere configuración TLS adicional (soporte de renegociación heredada y un bundle de CA personalizado) que el servicio maneja automáticamente cuando se especifica `"operator": "aire"`. Al ejecutar trabajos de Air-e de forma local (fuera de Docker), el bundle de CA debe configurarse mediante la variable de entorno `AIRE_CA_BUNDLE`.

---

## Restricción de Alojamiento (Render Plan Gratuito)

El servicio está alojado en el plan gratuito de Render, que apaga el servidor tras **15 minutos de inactividad**. Cualquier trabajo en segundo plano en curso se cancela cuando esto ocurre. Para evitarlo durante trabajos de larga duración, una función de mantenimiento debe hacer ping a `GET /health` cada 10 minutos. Esto se realiza mediante un disparador programado por tiempo en Google Apps Script.
