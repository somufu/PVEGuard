from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_wtf.csrf import CSRFProtect # type: ignore [import-untyped]
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timezone, timedelta, date as DateType
from collections import deque
import statistics
import math
from typing import List, Dict, Optional, Tuple, Deque, Any, Union

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", os.urandom(24))
csrf = CSRFProtect(app)

PROXMOX_HOST: Optional[str] = os.getenv("PROXMOX_HOST")
PROXMOX_USER: Optional[str] = os.getenv("PROXMOX_USER")
PROXMOX_PASSWORD: Optional[str] = os.getenv("PROXMOX_PASSWORD")
PROXMOX_VERIFY_SSL_STR: str = os.getenv("PROXMOX_VERIFY_SSL", "False")
PROXMOX_VERIFY_SSL: bool = PROXMOX_VERIFY_SSL_STR.lower() in ['true', '1', 't']

if not PROXMOX_VERIFY_SSL:
    if hasattr(requests.packages, 'urllib3'):
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning) # type: ignore

ProxmoxNodeType = Any
VMKeyType = Tuple[str, int]
CPUHistoryType = Deque[float]
RAMHistoryType = Deque[float]
PerfCPURAMHistoryType = Dict[str, Deque[float]]
PerformanceHistoryValueType = Dict[str, Union[PerfCPURAMHistoryType, str]]
PerformanceHistoryDictType = Dict[VMKeyType, PerformanceHistoryValueType]
VMConfigValueType = Dict[str, Union[Optional[str], Optional[int], str]]
CachedVMConfigsType = Dict[str, VMConfigValueType]
VMDetailType = Dict[str, Any]
SnapshotDetailType = Dict[str, Any]
RRDDataType = List[Dict[str, Optional[Union[int, float]]]]
ProcessedRRDValuesType = Dict[str, Optional[float]]

PERFORMANCE_HISTORY: PerformanceHistoryDictType = {}
CACHED_VM_CONFIGS: CachedVMConfigsType = {}

HISTORY_MAX_LEN: int = 10
CPU_UNDERUTILIZED_THRESHOLD: float = 20.0
RAM_UNDERUTILIZED_THRESHOLD: float = 30.0
CPU_SUGGESTION_THRESHOLD_PERCENT: float = 15.0
MIN_VCPU: int = 1
RAM_SUGGESTION_THRESHOLD_PERCENT: float = 25.0
MIN_RAM_MB: int = 1024

METRIC_DS_MAP: Dict[str, str] = {
    "cpu_usage_percent": "cpu",
    "ram_usage_percent": "mem",
    "diskread_Bps": "diskread",
    "diskwrite_Bps": "diskwrite",
    "netin_Bps": "netin",
    "netout_Bps": "netout",
}

def connect_to_proxmox() -> Optional[ProxmoxAPI]:
    try:
        if not all([PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD]):
            return None
        proxmox = ProxmoxAPI(
            PROXMOX_HOST, user=PROXMOX_USER, password=PROXMOX_PASSWORD,
            verify_ssl=PROXMOX_VERIFY_SSL, timeout=10
        )
        return proxmox
    except Exception as e:
        print(f"Proxmox bağlantı hatası: {str(e)}")
        return None

def get_vm_rrd_metrics(prox_instance: ProxmoxAPI, node: str, vmid: int, timeframe: str = 'hour') -> ProcessedRRDValuesType:
    metrics_to_fetch: Dict[str, str] = {
        ds_prox: key_fe for key_fe, ds_prox in METRIC_DS_MAP.items() if "_Bps" in key_fe or key_fe in ["cpu_usage_percent", "ram_usage_percent"]
    }
    output_keys_for_metrics: Dict[str,str] = {ds_prox: key_fe for key_fe, ds_prox in METRIC_DS_MAP.items()}
    processed_values: ProcessedRRDValuesType = {val: None for val in output_keys_for_metrics.values()}

    try:
        rrd_data_list: List[Dict[str, Any]] = prox_instance.nodes(node).qemu(vmid).rrddata.get(timeframe=timeframe, cf='AVERAGE')
        if not rrd_data_list: return processed_values
        for prox_ds_name, frontend_key_name in output_keys_for_metrics.items():
            latest_value: Optional[float] = None
            for data_point in reversed(rrd_data_list):
                if prox_ds_name in data_point and data_point[prox_ds_name] is not None:
                    try:
                        value = float(data_point[prox_ds_name])
                        if not math.isnan(value) and not math.isinf(value):
                            if prox_ds_name == "cpu": value *= 100
                            latest_value = round(value, 2); break
                    except (ValueError, TypeError): continue
            processed_values[frontend_key_name] = latest_value
    except requests.exceptions.HTTPError as e:
        if e.response and e.response.status_code == 500 and "rrdcached" in str(e.response.text).lower(): print(f"RRDcached error for VM {vmid} on {node} (timeframe: {timeframe}): {e}. Often transient.")
        elif e.response and e.response.status_code == 400 and "unknown data source" in str(e.response.text).lower():
            ds_name_error = str(e.response.text).split("'")[1] if "'" in str(e.response.text) else "unknown_ds"; print(f"RRD data source '{ds_name_error}' not found for VM {vmid} on {node}. Check METRIC_DS_MAP or RRD settings.")
        else: print(f"HTTPError fetching RRD data for VM {vmid} on {node} (timeframe: {timeframe}): {e}")
    except Exception as e: print(f"Generic error fetching RRD data for VM {vmid} on {node} (timeframe: {timeframe}): {e}")
    return processed_values

def get_vm_current_status(prox_instance: ProxmoxAPI, node_name: str, vmid: int) -> Optional[VMDetailType]:
    global PERFORMANCE_HISTORY
    if not prox_instance: return None
    vm_key: VMKeyType = (node_name, vmid)
    if vm_key not in PERFORMANCE_HISTORY: PERFORMANCE_HISTORY[vm_key] = {'history': {'cpu': deque(maxlen=HISTORY_MAX_LEN), 'ram': deque(maxlen=HISTORY_MAX_LEN)}, 'status_text': 'unknown'}
    current_status_text: str = 'unknown'; vm_perf_history: PerfCPURAMHistoryType = PERFORMANCE_HISTORY[vm_key]['history'] # type: ignore
    try:
        status: Dict[str, Any] = prox_instance.nodes(node_name).qemu(vmid).status.current.get()
        current_status_text = status.get('status', 'unknown'); PERFORMANCE_HISTORY[vm_key]['status_text'] = current_status_text
        rrd_metrics: ProcessedRRDValuesType = {};
        if current_status_text == 'running': rrd_metrics = get_vm_rrd_metrics(prox_instance, node_name, vmid, timeframe='hour')
        base_return_data: VMDetailType = {"vmid": vmid, "node": node_name, "status": current_status_text, "cpu_usage_percent": 0.0, "ram_usage_percent": 0.0, "avg_cpu_usage_percent": None, "max_cpu_usage_percent": None, "avg_ram_usage_percent": None, "max_ram_usage_percent": None, "history_count": 0, "name": status.get('name'), "uptime_seconds": status.get('uptime', 0), **rrd_metrics}
        if current_status_text != 'running':
            vm_perf_history['cpu'].clear(); vm_perf_history['ram'].clear(); base_return_data["history_count"] = 0; return base_return_data
        current_cpu_usage: float = status.get('cpu', 0.0) * 100.0; mem_used_bytes: int = status.get('mem', 0); max_mem_bytes: int = status.get('maxmem', 1); current_ram_usage: float = (mem_used_bytes / max_mem_bytes) * 100.0 if max_mem_bytes > 0 else 0.0
        vm_perf_history['cpu'].append(round(current_cpu_usage, 2)); vm_perf_history['ram'].append(round(current_ram_usage, 2))
        avg_cpu: Optional[float] = None; max_cpu: Optional[float] = None; avg_ram: Optional[float] = None; max_ram: Optional[float] = None
        cpu_history_list: List[float] = list(vm_perf_history['cpu']); ram_history_list: List[float] = list(vm_perf_history['ram'])
        if cpu_history_list: avg_cpu = round(statistics.mean(cpu_history_list), 2); max_cpu = round(max(cpu_history_list), 2)
        if ram_history_list: avg_ram = round(statistics.mean(ram_history_list), 2); max_ram = round(max(ram_history_list), 2)
        base_return_data.update({"cpu_usage_percent": round(current_cpu_usage, 2), "ram_usage_percent": round(current_ram_usage, 2), "mem_used_mb": round(mem_used_bytes / (1024 * 1024), 2), "max_mem_mb": round(max_mem_bytes / (1024 * 1024), 2), "avg_cpu_usage_percent": avg_cpu, "max_cpu_usage_percent": max_cpu, "avg_ram_usage_percent": avg_ram, "max_ram_usage_percent": max_ram, "history_count": len(cpu_history_list)})
        return base_return_data
    except requests.exceptions.HTTPError as e:
        error_message_str: str = f"HTTPError getting status for VM {vmid} on {node_name}: {e}"
        if e.response is not None:
            error_message_str += f" (Status: {e.response.status_code})"; response_text = getattr(e.response, 'text', '')
            if e.response.status_code == 500 and "qmp command 'query-status' failed" in response_text: current_status_text = 'error_transitional'
            elif e.response.status_code == 404: current_status_text = 'not_found'
            else: print(error_message_str)
        else: print(error_message_str)
        PERFORMANCE_HISTORY[vm_key]['status_text'] = current_status_text; vm_perf_history['cpu'].clear(); vm_perf_history['ram'].clear()
        return {"vmid": vmid, "node": node_name, "status": current_status_text, "cpu_usage_percent": 0.0, "ram_usage_percent": 0.0, "avg_cpu_usage_percent": None, "max_cpu_usage_percent": None, "avg_ram_usage_percent": None, "max_ram_usage_percent": None, "history_count": 0, "diskread_Bps": None, "diskwrite_Bps": None, "netin_Bps": None, "netout_Bps": None}
    except Exception as e:
        print(f"Generic error getting status for VM {vmid} on {node_name}: {e} (Type: {type(e)})"); PERFORMANCE_HISTORY[vm_key]['status_text'] = 'error_generic'; vm_perf_history['cpu'].clear(); vm_perf_history['ram'].clear()
        return {"vmid": vmid, "node": node_name, "status": 'error_generic', "cpu_usage_percent": 0.0, "ram_usage_percent": 0.0, "avg_cpu_usage_percent": None, "max_cpu_usage_percent": None, "avg_ram_usage_percent": None, "max_ram_usage_percent": None, "history_count": 0, "diskread_Bps": None, "diskwrite_Bps": None, "netin_Bps": None, "netout_Bps": None}

def get_all_vms_and_containers_with_initial_perf(prox_instance: ProxmoxAPI) -> List[VMDetailType]:
    global CACHED_VM_CONFIGS, PERFORMANCE_HISTORY;
    if not prox_instance: return []
    all_resources_details: List[VMDetailType] = []; CACHED_VM_CONFIGS.clear()
    try:
        nodes: List[Dict[str, Any]] = prox_instance.nodes.get()
        for node_info in nodes:
            node_name: str = node_info['node']
            try:
                vms_on_node: List[Dict[str, Any]] = prox_instance.nodes(node_name).qemu.get()
                for vm_item in vms_on_node:
                    vm_id: int = vm_item['vmid']; vm_key: VMKeyType = (node_name, vm_id)
                    if vm_key not in PERFORMANCE_HISTORY: PERFORMANCE_HISTORY[vm_key] = {'history': {'cpu': deque(maxlen=HISTORY_MAX_LEN), 'ram': deque(maxlen=HISTORY_MAX_LEN)}, 'status_text': 'unknown'}
                    vm_status_basic: str = vm_item.get('status', 'unknown'); vm_name_basic: str = vm_item.get('name', f"vm-{vm_id}"); current_config_raw: Dict[str, Any] = {}
                    try: current_config_raw = prox_instance.nodes(node_name).qemu(vm_id).config.get()
                    except Exception as conf_e: print(f"Could not get config for VM {vm_id} on {node_name}: {conf_e}")
                    current_vcpu: Optional[int] = current_config_raw.get('cores'); current_ram_mb: Optional[int] = current_config_raw.get('memory')
                    CACHED_VM_CONFIGS[str(vm_id)] = {'node': node_name, 'name': vm_name_basic, 'current_vcpu': current_vcpu, 'current_ram_mb': current_ram_mb, 'type': 'qemu'}
                    vm_detail: VMDetailType = {"node": node_name, "vmid": vm_id, "name": vm_name_basic, "type": "qemu", "status": vm_status_basic, "is_underutilized": False, "right_sizing_suggestion": "", "current_vcpu": current_vcpu, "current_ram_mb": current_ram_mb}
                    perf_data: Optional[VMDetailType] = get_vm_current_status(prox_instance, node_name, vm_id)
                    if perf_data: vm_detail.update(perf_data); vm_detail = calculate_right_sizing_suggestions(vm_detail)
                    all_resources_details.append(vm_detail)
            except Exception as e_qemu: print(f"Node {node_name} QEMU VM'leri alınırken hata: {e_qemu}")
            try:
                containers_on_node: List[Dict[str, Any]] = prox_instance.nodes(node_name).lxc.get()
                for ct_item in containers_on_node:
                    ct_id: int = ct_item['vmid']; ct_name: str = ct_item.get('name', f"ct-{ct_id}")
                    CACHED_VM_CONFIGS[str(ct_id)] = {'node': node_name, 'name': ct_name, 'type': 'lxc', 'current_vcpu': None, 'current_ram_mb': None}
                    all_resources_details.append({"node": node_name, "vmid": ct_id, "name": ct_name, "type": "lxc", "status": ct_item.get('status', 'unknown')})
            except Exception as e_lxc: print(f"Node {node_name} LXC konteynerleri alınırken hata: {e_lxc}")
        return all_resources_details
    except Exception as e: print(f"VM/CT listesi alınırken genel hata: {str(e)}"); return []

def calculate_right_sizing_suggestions(vm_detail: VMDetailType) -> VMDetailType:
    avg_cpu_usage: Optional[float] = vm_detail.get("avg_cpu_usage_percent"); avg_ram_usage: Optional[float] = vm_detail.get("avg_ram_usage_percent"); history_count: int = vm_detail.get("history_count", 0)
    current_vcpu_val: Optional[Union[int, str]] = vm_detail.get("current_vcpu"); current_ram_mb_val: Optional[Union[int, str]] = vm_detail.get("current_ram_mb")
    current_vcpu_int: Optional[int] = None; current_ram_mb_int: Optional[int] = None
    try:
        if current_vcpu_val is not None: current_vcpu_int = int(str(current_vcpu_val))
        if current_ram_mb_val is not None: current_ram_mb_int = int(str(current_ram_mb_val))
    except (ValueError, TypeError): pass
    vm_detail["is_underutilized"] = False; vm_detail["right_sizing_suggestion"] = ""
    if vm_detail.get("status") != "running": return vm_detail
    if avg_cpu_usage is not None and avg_ram_usage is not None and history_count >= (HISTORY_MAX_LEN / 2.0):
        suggestions: List[str] = []
        if current_vcpu_int is not None and current_vcpu_int > MIN_VCPU and avg_cpu_usage < CPU_SUGGESTION_THRESHOLD_PERCENT:
            suggested_vcpu: int = max(MIN_VCPU, current_vcpu_int - 1)
            if suggested_vcpu < current_vcpu_int: suggestions.append(f"vCPU: {current_vcpu_int} -> {suggested_vcpu}")
        if current_ram_mb_int is not None and current_ram_mb_int > MIN_RAM_MB and avg_ram_usage < RAM_SUGGESTION_THRESHOLD_PERCENT:
            suggested_ram_mb_step: int = current_ram_mb_int
            if current_ram_mb_int > 8192 and avg_ram_usage < (RAM_SUGGESTION_THRESHOLD_PERCENT - 10): suggested_ram_mb_step = current_ram_mb_int - 2048
            elif current_ram_mb_int > 4096: suggested_ram_mb_step = current_ram_mb_int - 1024
            elif current_ram_mb_int > 2048: suggested_ram_mb_step = current_ram_mb_int - 512
            elif current_ram_mb_int > MIN_RAM_MB + 256: suggested_ram_mb_step = current_ram_mb_int - 256
            suggested_ram_mb: int = max(MIN_RAM_MB, suggested_ram_mb_step)
            if suggested_ram_mb > 0 and suggested_ram_mb < current_ram_mb_int: suggestions.append(f"RAM: {current_ram_mb_int}MB -> {suggested_ram_mb}MB")
        if suggestions: vm_detail["right_sizing_suggestion"] = "; ".join(suggestions); vm_detail["is_underutilized"] = True
    return vm_detail

def get_snapshots_for_resource(prox_instance: ProxmoxAPI, node_name: str, vmid: int, resource_type: str, max_date_str: Optional[str] = None) -> List[SnapshotDetailType]:
    if not prox_instance: return []
    snapshots_data: List[SnapshotDetailType] = []; max_date_obj: Optional[DateType] = None
    if max_date_str:
        try: max_date_obj = datetime.strptime(max_date_str, "%Y-%m-%d").date()
        except ValueError: max_date_obj = None
    try:
        target_resource: ProxmoxNodeType = prox_instance.nodes(node_name).qemu(vmid) if resource_type == "qemu" else prox_instance.nodes(node_name).lxc(vmid)
        snapshots_raw: List[Dict[str, Any]] = target_resource.snapshot.get()
        for snap_info in snapshots_raw:
            create_time_unix: Optional[int] = snap_info.get('snaptime'); create_time_iso: Optional[str] = None; snapshot_date_obj: Optional[DateType] = None
            if create_time_unix: dt_obj: datetime = datetime.fromtimestamp(create_time_unix, tz=timezone.utc); create_time_iso = dt_obj.isoformat(); snapshot_date_obj = dt_obj.date()
            add_snapshot: bool = True
            if max_date_obj:
                if snapshot_date_obj:
                    if snapshot_date_obj > max_date_obj: add_snapshot = False
                elif str(snap_info.get('name', '')).lower() != 'current': add_snapshot = False
            if add_snapshot: snapshots_data.append({"node": node_name, "vmid": vmid, "resource_type": resource_type, "snap_name": snap_info.get('name', 'UnknownSnapName'), "description": snap_info.get('description', ''), "parent": snap_info.get('parent'), "vmstate": bool(snap_info.get('vmstate', False)), "create_time_unix": create_time_unix, "create_time_iso": create_time_iso})
        if snapshots_data: snapshots_data.sort(key=lambda x: (x.get('create_time_unix') is None, x.get('create_time_unix', float('inf'))))
    except requests.exceptions.HTTPError as e:
        error_text_str: str = ""; response_status_code: Optional[int] = None; response_text_content: str = ''
        if e.response is not None: error_text_str = str(e.response.text).lower() if e.response.text else ""; response_status_code = e.response.status_code; response_text_content = e.response.text
        else: error_text_str = str(e).lower()
        if not ("no snapshots found" in error_text_str or "does not exist" in error_text_str or "no such file or directory" in error_text_str or (response_status_code == 500 and "no configuration file" in response_text_content) or (response_status_code == 400 and "not a snapshot name" in response_text_content)):
            print(f"Snapshot'lar alınırken HTTPError (N: {node_name}, ID: {vmid}, T: {resource_type}): {e}")
            if response_status_code is not None: print(f"API Yanıtı: {response_status_code} - {response_text_content}")
    except Exception as e:
        print(f"Snapshot'lar alınırken Genel Hata (N: {node_name}, ID: {vmid}, T: {resource_type}): {e} (Type: {type(e)})")
        if hasattr(e, 'response') and e.response and hasattr(e.response, 'status_code'): print(f"API Yanıtı: {e.response.status_code} - {e.response.text}") # type: ignore
    return snapshots_data

def get_all_snapshots_up_to_date(prox_instance: ProxmoxAPI, all_resources_list: List[VMDetailType], max_date_str: Optional[str] = None) -> List[SnapshotDetailType]:
    if not prox_instance or not all_resources_list: return []
    all_snapshots: List[SnapshotDetailType] = []
    for res in all_resources_list:
        node: str = str(res['node']); vmid: int = int(res['vmid']); res_type: str = str(res['type']); res_name: Optional[Any] = res.get('name'); vm_status: str = str(res.get('status', 'unknown'))
        resource_snapshots: List[SnapshotDetailType] = get_snapshots_for_resource(prox_instance, node, vmid, res_type, max_date_str)
        for snap in resource_snapshots:
            if str(snap.get('snap_name', '')).lower() == 'current': continue
            snap['resource_name'] = str(res_name) if res_name else f"{res_type}-{vmid}"; snap['vm_status'] = vm_status; all_snapshots.append(snap)
    return all_snapshots

@app.route('/', methods=['GET'])
def index() -> str:
    prox_conn: Optional[ProxmoxAPI] = connect_to_proxmox(); snapshots_list_final: List[SnapshotDetailType] = []; all_qemu_vms_with_state_for_template: List[VMDetailType] = []; error_message: Optional[str] = None
    underutilized_vms_count: int = 0; idle_vms_count: int = 0; orphaned_old_root_snapshot_count: int = 0
    max_date_filter_req: Optional[str] = request.args.get('max_date'); sort_by_req: str = request.args.get('sort_by', 'date'); order_req: str = request.args.get('order', 'desc')
    old_snapshot_days_threshold_val: int; idle_vm_threshold_days_val: int
    try: old_snapshot_days_threshold_val = int(request.args.get('old_days', '30')); old_snapshot_days_threshold_val = 30 if old_snapshot_days_threshold_val < -1 else old_snapshot_days_threshold_val
    except (ValueError, TypeError): old_snapshot_days_threshold_val = 30
    try: idle_vm_threshold_days_val = int(request.args.get('idle_days', '60')); idle_vm_threshold_days_val = 60 if idle_vm_threshold_days_val < 0 else idle_vm_threshold_days_val
    except (ValueError, TypeError): idle_vm_threshold_days_val = 60
    current_params: Dict[str, Union[str, int]] = {'max_date': max_date_filter_req or '', 'sort_by': sort_by_req, 'order': order_req, 'old_days': old_snapshot_days_threshold_val, 'idle_days': idle_vm_threshold_days_val}
    if prox_conn:
        try:
            all_resources_with_initial_perf: List[VMDetailType] = get_all_vms_and_containers_with_initial_perf(prox_conn)
            all_qemu_vms_with_state_for_template = [vm for vm in all_resources_with_initial_perf if vm.get('type') == 'qemu']
            snapshots_list_raw_unfiltered_no_current: List[SnapshotDetailType] = get_all_snapshots_up_to_date(prox_conn, all_resources_with_initial_perf, None)
            now_utc: datetime = datetime.now(timezone.utc); snapshots_by_vm_key_type = Tuple[str, int, str]; snapshots_by_vm: Dict[snapshots_by_vm_key_type, List[SnapshotDetailType]] = {}
            for snap_raw in snapshots_list_raw_unfiltered_no_current:
                node_for_key = str(snap_raw['node']); vmid_for_key = int(snap_raw['vmid']); type_for_key = str(snap_raw['resource_type']); vm_key_snap: snapshots_by_vm_key_type = (node_for_key, vmid_for_key, type_for_key)
                if vm_key_snap not in snapshots_by_vm: snapshots_by_vm[vm_key_snap] = []
                snap: SnapshotDetailType = snap_raw.copy(); snap['is_old'] = False; snap['is_on_stopped_vm_and_old'] = False; snap['is_orphaned_old_root'] = False
                snap_create_time: Optional[int] = snap.get('create_time_unix')
                if snap_create_time:
                    age: timedelta = now_utc - datetime.fromtimestamp(snap_create_time, tz=timezone.utc)
                    if old_snapshot_days_threshold_val == -1 or age.days > old_snapshot_days_threshold_val:
                        snap['is_old'] = True
                        if str(snap.get('vm_status', '')).lower() == 'stopped': snap['is_on_stopped_vm_and_old'] = True
                snapshots_by_vm[vm_key_snap].append(snap)
            original_snapshots_for_current_check_key_type = Tuple[str, int, str]; original_snapshots_for_current_check: Dict[original_snapshots_for_current_check_key_type, List[SnapshotDetailType]] = {}
            for res_orig in all_resources_with_initial_perf:
                res_key: original_snapshots_for_current_check_key_type = (str(res_orig['node']), int(res_orig['vmid']), str(res_orig['type']))
                original_snapshots_for_current_check[res_key] = get_snapshots_for_resource(prox_conn, str(res_orig['node']), int(res_orig['vmid']), str(res_orig['type']), None)
            for vm_key_tuple_iter, vm_snapshots_list_for_vm in snapshots_by_vm.items():
                current_snap_from_api_list_iter: List[SnapshotDetailType] = original_snapshots_for_current_check.get(vm_key_tuple_iter, [])
                current_snap_obj_iter: Optional[SnapshotDetailType] = next((s for s in current_snap_from_api_list_iter if str(s.get('snap_name','')).lower() == 'current'), None)
                active_ancestors_names_set: set[str] = set(); current_parent_name_str: Optional[str] = None
                if current_snap_obj_iter and current_snap_obj_iter.get('parent'):
                    current_parent_name_str = str(current_snap_obj_iter.get('parent')); temp_parent_name_str: Optional[str] = current_parent_name_str
                    all_snaps_for_this_vm_map_with_current_iter: Dict[str, SnapshotDetailType] = {str(s.get('snap_name','')): s for s in current_snap_from_api_list_iter if s.get('snap_name')}
                    while temp_parent_name_str and temp_parent_name_str in all_snaps_for_this_vm_map_with_current_iter:
                        active_ancestors_names_set.add(temp_parent_name_str); parent_obj_iter: Optional[SnapshotDetailType] = all_snaps_for_this_vm_map_with_current_iter.get(temp_parent_name_str)
                        if not parent_obj_iter: break
                        temp_parent_name_str = str(parent_obj_iter.get('parent')) if parent_obj_iter.get('parent') else None
                for snap_to_check_iter in vm_snapshots_list_for_vm:
                    if snap_to_check_iter.get('is_old') and not snap_to_check_iter.get('parent'):
                        snap_name_str_check: str = str(snap_to_check_iter.get('snap_name',''))
                        if snap_name_str_check != current_parent_name_str and snap_name_str_check not in active_ancestors_names_set:
                            snap_to_check_iter['is_orphaned_old_root'] = True
            processed_snapshots_for_display: List[SnapshotDetailType] = []; max_date_obj_filter_val: Optional[DateType] = None
            if max_date_filter_req:
                try: max_date_obj_filter_val = datetime.strptime(max_date_filter_req, "%Y-%m-%d").date()
                except ValueError: max_date_obj_filter_val = None
            temp_orphaned_count: int = 0
            for vm_key_from_map_iter_disp, snaps_in_vm_iter_disp in snapshots_by_vm.items():
                for s_processed_iter_disp in snaps_in_vm_iter_disp:
                    include_in_final_list_bool_disp: bool = True; s_processed_time_unix_disp: Optional[int] = s_processed_iter_disp.get('create_time_unix')
                    if max_date_obj_filter_val and s_processed_time_unix_disp:
                        snap_date_val_disp: DateType = datetime.fromtimestamp(s_processed_time_unix_disp, tz=timezone.utc).date()
                        if snap_date_val_disp > max_date_obj_filter_val: include_in_final_list_bool_disp = False
                    if include_in_final_list_bool_disp:
                        processed_snapshots_for_display.append(s_processed_iter_disp)
                        if s_processed_iter_disp.get('is_orphaned_old_root'): temp_orphaned_count += 1
            orphaned_old_root_snapshot_count = temp_orphaned_count; underutilized_vms_count = 0
            for vm_iter_template_counts in all_qemu_vms_with_state_for_template:
                if vm_iter_template_counts.get('is_underutilized'): underutilized_vms_count +=1
                vm_iter_template_counts['is_potentially_idle'] = False
                if str(vm_iter_template_counts.get('status','')).lower() == 'stopped':
                    vm_snaps_for_idle_list_counts: List[SnapshotDetailType] = [s_idle_c for s_idle_c in snapshots_list_raw_unfiltered_no_current if str(s_idle_c.get('node')) == str(vm_iter_template_counts.get('node')) and str(s_idle_c.get('vmid')) == str(vm_iter_template_counts.get('vmid')) and s_idle_c.get('create_time_unix') is not None]
                    if vm_snaps_for_idle_list_counts:
                        valid_snap_times_counts = [s_val_c['create_time_unix'] for s_val_c in vm_snaps_for_idle_list_counts if s_val_c.get('create_time_unix') is not None]
                        if valid_snap_times_counts:
                            latest_snap_time_val_counts: int = max(valid_snap_times_counts)
                            age_latest_snap_val_counts: timedelta = now_utc - datetime.fromtimestamp(latest_snap_time_val_counts, tz=timezone.utc)
                            if age_latest_snap_val_counts.days > idle_vm_threshold_days_val: vm_iter_template_counts['is_potentially_idle'] = True
            idle_vms_count = sum(1 for vm_data_iter_counts in all_qemu_vms_with_state_for_template if vm_data_iter_counts.get('is_potentially_idle'))
            reverse_order_bool: bool = (order_req == 'desc')
            if not processed_snapshots_for_display: snapshots_list_final = []
            else:
                if sort_by_req == 'name': snapshots_list_final = sorted(processed_snapshots_for_display, key=lambda x_sort: str(x_sort.get('snap_name','')).lower(), reverse=reverse_order_bool)
                elif sort_by_req == 'resource': snapshots_list_final = sorted(processed_snapshots_for_display, key=lambda x_sort: (str(x_sort.get('resource_name', '')).lower(), x_sort.get('create_time_unix', float('-inf' if reverse_order_bool else 'inf'))), reverse=reverse_order_bool)
                else: snapshots_list_final = sorted(processed_snapshots_for_display, key=lambda x_sort: (x_sort.get('create_time_unix') is None, x_sort.get('create_time_unix', float('-inf' if reverse_order_bool else 'inf'))), reverse=reverse_order_bool)
        except Exception as e: print(f"Ana sayfada veri alınırken hata: {e}, {type(e)}"); import traceback; traceback.print_exc(); error_message = f"Veri alınırken bir hata oluştu: {str(e)}"
    else: error_message = "Proxmox VE sunucusuna bağlanılamadı."
    current_params_for_template = {k: (str(v) if v is not None else '') for k, v in current_params.items()}
    return render_template('index.html', snapshots=snapshots_list_final, all_qemu_vms=all_qemu_vms_with_state_for_template, underutilized_vms_count=underutilized_vms_count, idle_vms_count=idle_vms_count, orphaned_old_root_snapshot_count=orphaned_old_root_snapshot_count, idle_vm_threshold_days=idle_vm_threshold_days_val, cpu_suggestion_threshold=CPU_SUGGESTION_THRESHOLD_PERCENT, ram_suggestion_threshold=RAM_SUGGESTION_THRESHOLD_PERCENT, history_max_len=HISTORY_MAX_LEN, performance_history_data=PERFORMANCE_HISTORY, error=error_message, current_params=current_params_for_template, current_max_date=str(current_params_for_template.get('max_date','')), sort_by=str(current_params_for_template.get('sort_by','date')), order=str(current_params_for_template.get('order','desc')), current_old_days=int(str(current_params_for_template.get('old_days','30'))))

@app.route('/api/live_vm_performance', methods=['GET'])
def api_live_vm_performance() -> Any:
    global PERFORMANCE_HISTORY, CACHED_VM_CONFIGS; prox_conn: Optional[ProxmoxAPI] = connect_to_proxmox(); live_performance_data_response: Dict[str, VMDetailType] = {}
    if not prox_conn: return jsonify({"error": "Proxmox VE sunucusuna bağlanılamadı."}), 503
    vms_to_update_keys: List[VMKeyType] = list(PERFORMANCE_HISTORY.keys())
    for vm_key_api in vms_to_update_keys:
        node_name_api, vmid_int_api = vm_key_api; vmid_str_api: str = str(vmid_int_api)
        if vm_key_api not in PERFORMANCE_HISTORY: PERFORMANCE_HISTORY[vm_key_api] = {'history': {'cpu': deque(maxlen=HISTORY_MAX_LEN), 'ram': deque(maxlen=HISTORY_MAX_LEN)}, 'status_text': 'unknown'}
        vm_perf_hist_api: PerfCPURAMHistoryType = PERFORMANCE_HISTORY[vm_key_api]['history'] # type: ignore
        vm_config_api: Optional[VMConfigValueType] = CACHED_VM_CONFIGS.get(vmid_str_api)
        if not vm_config_api or vm_config_api.get('type') != 'qemu': continue
        perf_data_api: Optional[VMDetailType] = get_vm_current_status(prox_conn, node_name_api, vmid_int_api)
        if perf_data_api:
            history_count_val_api = perf_data_api.get('history_count', 0)
            api_vm_data_dict: VMDetailType = {'status': perf_data_api.get('status'), 'cpu_usage_percent': perf_data_api.get('cpu_usage_percent'), 'ram_usage_percent': perf_data_api.get('ram_usage_percent'), 'avg_cpu_usage_percent': perf_data_api.get('avg_cpu_usage_percent'), 'max_cpu_usage_percent': perf_data_api.get('max_cpu_usage_percent'), 'avg_ram_usage_percent': perf_data_api.get('avg_ram_usage_percent'), 'max_ram_usage_percent': perf_data_api.get('max_ram_usage_percent'), 'cpu_history': list(vm_perf_hist_api['cpu']), 'ram_history': list(vm_perf_hist_api['ram']), 'diskread_Bps': perf_data_api.get('diskread_Bps'), 'diskwrite_Bps': perf_data_api.get('diskwrite_Bps'), 'netin_Bps': perf_data_api.get('netin_Bps'), 'netout_Bps': perf_data_api.get('netout_Bps')}
            temp_suggestion_data_api: VMDetailType = {**api_vm_data_dict, 'current_vcpu': vm_config_api.get('current_vcpu'), 'current_ram_mb': vm_config_api.get('current_ram_mb'), 'history_count': history_count_val_api}
            updated_suggestion_data_api: VMDetailType = calculate_right_sizing_suggestions(temp_suggestion_data_api)
            api_vm_data_dict['is_underutilized'] = updated_suggestion_data_api.get('is_underutilized', False); api_vm_data_dict['right_sizing_suggestion'] = updated_suggestion_data_api.get('right_sizing_suggestion', '')
            live_performance_data_response[vmid_str_api] = api_vm_data_dict
        else:
            error_status_api: str = str(PERFORMANCE_HISTORY.get(vm_key_api, {}).get('status_text', 'error_unknown'))
            live_performance_data_response[vmid_str_api] = {'status': error_status_api, 'cpu_usage_percent': 0.0,'ram_usage_percent': 0.0, 'avg_cpu_usage_percent': None, 'max_cpu_usage_percent': None, 'avg_ram_usage_percent': None, 'max_ram_usage_percent': None, 'is_underutilized': False, 'right_sizing_suggestion': '', 'cpu_history': [], 'ram_history': [], 'diskread_Bps': None, 'diskwrite_Bps': None, 'netin_Bps': None, 'netout_Bps': None}
            if error_status_api not in ['running', 'stopped'] and vm_key_api in PERFORMANCE_HISTORY: vm_perf_hist_api['cpu'].clear(); vm_perf_hist_api['ram'].clear()
    return jsonify(live_performance_data_response)

@app.route('/api/vm_metric_history/<int:vmid>/<metric_name>', methods=['GET'])
def api_vm_metric_history(vmid: int, metric_name: str) -> Any:
    prox_conn: Optional[ProxmoxAPI] = connect_to_proxmox()
    if not prox_conn: return jsonify({"error": "Proxmox bağlantısı kurulamadı"}), 503
    timeframe: str = request.args.get('timeframe', 'hour')
    if timeframe not in ['hour', 'day', 'week', 'month', 'year']: return jsonify({"error": f"Geçersiz zaman aralığı: {timeframe}"}), 400
    vm_config = CACHED_VM_CONFIGS.get(str(vmid))
    if not vm_config or vm_config.get('type') != 'qemu': return jsonify({"error": f"VM {vmid} bulunamadı veya QEMU değil."}), 404
    node_name: str = str(vm_config.get('node'))
    ds_name: Optional[str] = METRIC_DS_MAP.get(metric_name)
    if metric_name == "cpu_usage_percent_short" or metric_name == "ram_usage_percent_short":
        vm_key_hist = (node_name, vmid); history_key_hist = 'cpu' if metric_name == "cpu_usage_percent_short" else 'ram'
        history_data_hist: Deque[float] = PERFORMANCE_HISTORY.get(vm_key_hist, {}).get('history', {}).get(history_key_hist, deque()) # type: ignore
        return jsonify({"labels": list(range(len(history_data_hist))), "values": list(history_data_hist), "ds_name_used": metric_name})
    if not ds_name: return jsonify({"error": f"Bilinmeyen metrik adı: {metric_name}. METRIC_DS_MAP'i kontrol edin."}), 400
    labels: List[int] = []; values: List[Optional[float]] = []
    try:
        rrd_data: List[Dict[str, Any]] = prox_conn.nodes(node_name).qemu(vmid).rrddata.get(timeframe=timeframe, cf='AVERAGE')
        max_mem_bytes_for_ram_pct: Optional[float] = None
        if ds_name == "mem":
            try: status_current = prox_conn.nodes(node_name).qemu(vmid).status.current.get(); max_mem_bytes_for_ram_pct = float(status_current.get('maxmem', 0))
            except Exception: print(f"Could not get maxmem for VM {vmid} for RAM % calculation.")
        for data_point in rrd_data:
            timestamp: Optional[int] = data_point.get('time'); value_raw: Optional[Union[str, float, int]] = data_point.get(ds_name)
            if timestamp is None or value_raw is None: continue
            value: Optional[float] = None
            try: value = float(value_raw)
            except (ValueError, TypeError): value = None
            if value is not None and (math.isnan(value) or math.isinf(value)): value = None
            if value is not None:
                 if ds_name == "cpu": value = round(value * 100, 2)
                 elif ds_name == "mem":
                    if max_mem_bytes_for_ram_pct and max_mem_bytes_for_ram_pct > 0: value = round((value / max_mem_bytes_for_ram_pct) * 100, 2)
                    else: value = None
                 elif "_Bps" in metric_name: value = round(value, 2)
            labels.append(timestamp); values.append(value)
        return jsonify({"labels": labels, "values": values, "ds_name_used": ds_name})
    except requests.exceptions.HTTPError as e_rrd:
        if e_rrd.response and e_rrd.response.status_code == 400 and "unknown data source" in str(e_rrd.response.text).lower(): return jsonify({"error": f"RRD veri kaynağı '{ds_name}' bulunamadı (VM: {vmid}, Node: {node_name}). METRIC_DS_MAP'i kontrol edin."}), 400
        print(f"RRD verisi çekilirken HTTP Hatası (VMID: {vmid}, Metrik: {metric_name}): {e_rrd}"); return jsonify({"error": f"RRD verisi çekilemedi: {e_rrd.response.status_code if e_rrd.response else 'Bilinmeyen HTTP Hatası'}"}), 500
    except Exception as e_gen: print(f"RRD verisi çekilirken genel hata (VMID: {vmid}, Metrik: {metric_name}): {e_gen}"); return jsonify({"error": "RRD verisi çekilirken genel bir hata oluştu."}), 500

@app.route('/vm_action/<node>/<int:vmid>/<action>', methods=['POST'])
def vm_action_route(node: str, vmid: int, action: str) -> Any:
    response_data: Dict[str, Any] = {"status": "error", "message": "Bilinmeyen bir hata oluştu.", "category": "error", "vmid": vmid, "node": node, "new_status": None}
    prox_conn: Optional[ProxmoxAPI] = connect_to_proxmox()
    if not prox_conn: response_data["message"] = "Proxmox VE sunucusuna bağlanılamadı."; return jsonify(response_data), 503
    vm_name_default: str = f"VM {vmid}"; vm_name: str = vm_name_default
    try:
        resource_vm: ProxmoxNodeType = prox_conn.nodes(node).qemu(vmid)
        try: config_data: Dict[str, Any] = resource_vm.config.get(); vm_name = str(config_data.get('name', vm_name_default))
        except Exception: pass
        action_map: Dict[str, Any] = {'start': resource_vm.status.start.post, 'stop': resource_vm.status.stop.post, 'reboot': resource_vm.status.reboot.post, 'shutdown': resource_vm.status.shutdown.post}
        if action in action_map:
            task_id: str = str(action_map[action]()); new_status_guess: str = 'running' if action in ['start', 'reboot'] else 'stopped'
            response_data.update({"status": "success", "message": f'"{vm_name}" (VMID: {vmid}) için "{action}" işlemi tetiklendi. (Görev: {task_id})', "category": "success", "new_status": new_status_guess})
            return jsonify(response_data), 200
        else: response_data["message"] = 'Geçersiz VM işlemi.'; return jsonify(response_data), 400
    except Exception as e:
        err_msg_user: str = str(e)
        if hasattr(e, 'args') and e.args and isinstance(e.args[0], str): err_msg_user = e.args[0]
        response_data["message"] = f'VM {vmid} ("{vm_name}") için "{action}" hatası: {err_msg_user}'
        print(f"Exception VM {vmid} işlem {action}: {e}")
        if hasattr(e, 'response') and e.response and hasattr(e.response, 'status_code'): print(f"API Detayı: {e.response.status_code} - {e.response.text}") # type: ignore
        try: current_status_data_exc: Dict[str, Any] = prox_conn.nodes(node).qemu(vmid).status.current.get(); response_data["new_status"] = current_status_data_exc.get('status')
        except: pass
        return jsonify(response_data), 500

@app.route('/delete_snapshots', methods=['POST'])
def delete_snapshots_route() -> Any:
    selected_snapshots_raw: List[str] = request.form.getlist('selected_snapshots')
    response_data: Dict[str, Any] = {"status": "error", "message": "Bir hata oluştu.", "deleted_snapshots": [], "errors": [], "category": "error"}
    if not selected_snapshots_raw: response_data["message"] = "Silmek için en az bir snapshot seçmelisiniz."; response_data["category"] = "warning"; return jsonify(response_data), 400
    prox_conn: Optional[ProxmoxAPI] = connect_to_proxmox()
    if not prox_conn: response_data["message"] = "Proxmox VE sunucusuna bağlanılamadı. Snapshotlar silinemedi."; return jsonify(response_data), 503
    success_count: int = 0; error_count: int = 0
    for snap_identifier_del in selected_snapshots_raw:
        try:
            parts_del: List[str] = snap_identifier_del.split('/')
            if len(parts_del) != 4: response_data["errors"].append({"id": snap_identifier_del, "error": "Geçersiz format"}); error_count += 1; continue
            node_snap_del: str = parts_del[0]; resource_type_snap_del: str = parts_del[1]; vmid_str_snap_del: str = parts_del[2]; snap_name_del: str = parts_del[3]; vmid_snap_del: int = int(vmid_str_snap_del)
            target_resource_snap_del: ProxmoxNodeType = prox_conn.nodes(node_snap_del).qemu(vmid_snap_del) if resource_type_snap_del == "qemu" else prox_conn.nodes(node_snap_del).lxc(vmid_snap_del)
            target_resource_snap_del.snapshot(snap_name_del).delete(); response_data["deleted_snapshots"].append(snap_identifier_del); success_count += 1
        except Exception as e:
            error_count += 1; err_msg_del: str = str(e)
            if hasattr(e, 'args') and e.args and isinstance(e.args[0], str): err_msg_del = e.args[0]
            if hasattr(e, 'response') and e.response and hasattr(e.response, 'status_code'): err_msg_del += f" (API: {e.response.status_code} - {e.response.text})" # type: ignore
            response_data["errors"].append({"id": snap_identifier_del, "error": err_msg_del}); print(f"Snapshot silinirken hata ({snap_identifier_del}): {err_msg_del}")
    if success_count > 0 and error_count == 0: response_data.update({"status": "success", "message": f'{success_count} snapshot için silme işlemi başarıyla tetiklendi.', "category": "success"})
    elif success_count > 0 and error_count > 0: response_data.update({"status": "partial_success", "message": f'{success_count} snapshot silindi, {error_count} tanesi silinirken hata oluştu.', "category": "warning"})
    elif error_count > 0: response_data.update({"status": "error", "message": f'{error_count} snapshot silinirken hata oluştu.', "category": "error"})
    elif not response_data["errors"]: response_data.update({"status": "info", "message": "Silinecek uygun snapshot bulunamadı veya işlem yapılmadı.", "category": "info"})
    return jsonify(response_data)

@app.route('/about', methods=['GET'])
def about_page() -> str:
    return render_template('about.html')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)