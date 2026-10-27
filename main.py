from fastapi import FastAPI, UploadFile, File, HTTPException
import pdfplumber
import io
import re

app = FastAPI(title="Flota PDF Extractor")

@app.post("/extract-schedule")
async def extract_schedule(file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    contents = await file.read()
    
    # Guardar copia local para auditoría y pruebas
    try:
        with open("uploaded_manifest.pdf", "wb") as f_out:
            f_out.write(contents)
    except Exception:
        pass

    all_individual_trips = []

    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            # Recorrer todas las páginas
            for page_idx, page in enumerate(pdf.pages, start=1):
                found_tables = page.find_tables() or []

                for t_obj in found_tables:
                    table = t_obj.extract()
                    if not table:
                        continue

                    # 1. Identificar dinámicamente el índice de cada columna según encabezados
                    header_map = {}
                    header_row_idx = -1

                    for r_idx, row in enumerate(table):
                        row_str = " ".join([str(c) for c in row if c])
                        if "nº transp." in row_str.lower() or "número ast" in row_str.lower() or "transp." in row_str.lower():
                            header_row_idx = r_idx
                            for c_idx, cell in enumerate(row):
                                if not cell:
                                    continue
                                c_clean = str(cell).strip().lower()
                                
                                # Prioridad en mapeo de columnas para evitar falsos positivos
                                if "medio de transporte" in c_clean:
                                    header_map["medio_transporte"] = c_idx
                                elif "hora fin" in c_clean or ("hora" in c_clean and "fin" in c_clean):
                                    header_map["hora_f"] = c_idx
                                elif "hora in" in c_clean or ("hora" in c_clean and ("in" in c_clean or "ini" in c_clean)):
                                    header_map["hora_i"] = c_idx
                                elif "fe.final" in c_clean or ("final" in c_clean and "hora" not in c_clean):
                                    header_map["fecha_f"] = c_idx
                                elif "fe.inicio" in c_clean or ("inicio" in c_clean and "hora" not in c_clean):
                                    header_map["fecha_i"] = c_idx
                                elif "nº transp" in c_clean or "no transp" in c_clean or "nro transp" in c_clean or (c_clean.startswith("n") and "transp" in c_clean):
                                    header_map["guia_viaje"] = c_idx
                                elif "ast" in c_clean:
                                    header_map["ast"] = c_idx
                                elif "descrubic" in c_clean:
                                    header_map["cod_origen"] = c_idx
                                elif "desc.orig" in c_clean or ("orig" in c_clean and "desc" in c_clean):
                                    header_map["punto_origen"] = c_idx
                                elif "desc.dest" in c_clean or ("dest" in c_clean and "desc" in c_clean):
                                    header_map["punto_destino"] = c_idx
                                elif "ubicaci" in c_clean:
                                    header_map["cod_destino"] = c_idx
                                elif "recurso" in c_clean:
                                    header_map["recurso"] = c_idx
                                elif any(k in c_clean for k in ["observac", "observ", "obs", "coment", "nota"]):
                                    header_map["observacion"] = c_idx
                                elif "documento" in c_clean:
                                    header_map["documento"] = c_idx

                            # Asignar la última columna como 'observacion' si no se detectó explícitamente
                            if "observacion" not in header_map and len(row) > 0:
                                header_map["observacion"] = len(row) - 1
                            break

                    # Si esta tabla no tiene encabezados de transporte, continuar con la siguiente
                    if header_row_idx == -1 or "guia_viaje" not in header_map:
                        continue

                    # 2. Procesar filas de datos posteriores al encabezado
                    for r_idx in range(header_row_idx + 1, len(table)):
                        row = table[r_idx]
                        if not row or all(c is None or str(c).strip() == "" for c in row):
                            continue

                        # Separar celdas con saltos de línea (múltiples viajes en una celda)
                        row_splits = {}
                        max_lines = 1
                        for key, c_idx in header_map.items():
                            cell_val = row[c_idx] if c_idx < len(row) and row[c_idx] is not None else ""
                            lines = [line.strip() for line in str(cell_val).split("\n")]
                            row_splits[key] = lines
                            if len(lines) > max_lines:
                                max_lines = len(lines)

                        # Si hay múltiples líneas agrupadas en la fila, alinear celdas con menos líneas usando coordenadas de celda
                        if max_lines > 1 and r_idx < len(t_obj.rows):
                            t_row = t_obj.rows[r_idx]
                            for key, c_idx in header_map.items():
                                lines = row_splits.get(key, [])
                                if 0 < len(lines) < max_lines and c_idx < len(t_row.cells) and t_row.cells[c_idx]:
                                    cell_bbox = t_row.cells[c_idx]
                                    cell_words = page.crop(cell_bbox).extract_words()
                                    if cell_words:
                                        c_top, c_bot = cell_bbox[1], cell_bbox[3]
                                        h = max(1.0, c_bot - c_top)
                                        slot_words = [[] for _ in range(max_lines)]
                                        for w in cell_words:
                                            mid_y = (w["top"] + w["bottom"]) / 2.0
                                            s_idx = int((mid_y - c_top) / (h / max_lines))
                                            s_idx = min(max(0, s_idx), max_lines - 1)
                                            slot_words[s_idx].append(w["text"])
                                        row_splits[key] = [" ".join(sw).strip() for sw in slot_words]

                        for l_idx in range(max_lines):
                            def get_val(k):
                                lines = row_splits.get(k, [])
                                if l_idx < len(lines) and lines[l_idx]:
                                    return lines[l_idx]
                                # Para datos comunes de fila como origen o descripción de medio, heredar si solo hay 1 línea
                                if k in ["cod_origen", "punto_origen", "medio_transporte"] and len(lines) == 1 and lines[0]:
                                    return lines[0]
                                return ""

                            g_viaje = get_val("guia_viaje")
                            # Validar que contenga números de despacho
                            if not g_viaje or not re.search(r"\d", g_viaje):
                                continue

                            # Normalizar fechas a formato DD/MM/YYYY
                            raw_fi = get_val("fecha_i").replace(".", "/")
                            raw_ff = get_val("fecha_f").replace(".", "/")

                            def format_hora(h_str):
                                if not h_str:
                                    return "06:00 AM"
                                m = re.match(r"^(\d{1,2}):(\d{2})", h_str)
                                if m:
                                    h = int(m.group(1))
                                    mn = m.group(2)
                                    ampm = "AM" if h < 12 else "PM"
                                    h12 = h if h <= 12 else h - 12
                                    if h12 == 0:
                                        h12 = 12
                                    return f"{h12:02d}:{mn} {ampm}"
                                return h_str

                            # Observación toma estrictamente la última columna / columna de Observación del manifiesto
                            obs = get_val("observacion") or ""

                            trip_item = {
                                "guia_viaje": g_viaje,
                                "cod_origen": get_val("cod_origen") or "0704",
                                "punto_origen": get_val("punto_origen") or "Planta Origen",
                                "fecha_i": raw_fi,
                                "hora_i": format_hora(get_val("hora_i")),
                                "cod_destino": get_val("cod_destino") or "C010",
                                "punto_destino": get_val("punto_destino") or "Destino",
                                "fecha_f": raw_ff or raw_fi,
                                "hora_f": format_hora(get_val("hora_f") or get_val("hora_i")),
                                "recurso": get_val("recurso") or "",
                                "medio_transporte": get_val("medio_transporte") or "",
                                "observacion": obs
                            }
                            all_individual_trips.append(trip_item)

    except Exception as e:
        print(f"Error procesando PDF: {e}")
        raise HTTPException(status_code=500, detail=f"Error al analizar estructura del PDF: {str(e)}")

    return {
        "total": len(all_individual_trips),
        "trips": all_individual_trips
    }