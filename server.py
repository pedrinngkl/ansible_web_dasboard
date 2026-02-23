import os
import time
import json
import subprocess
import mysql.connector
from flask import Flask, Response, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv 

# Carrega as variáveis do arquivo .env para a memória do sistema
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- CONFIGURAÇÕES DE BANCO E CAMINHOS NATIVOS ---
db_config = {
    'user': os.getenv('DB_USER', 'root'), # O segundo valor é um fallback caso não ache
    'password': os.getenv('DB_PASSWORD', ''), 
    'host': os.getenv('DB_HOST', 'localhost'),
    'database': os.getenv('DB_NAME', 'ansible_web')
}

PLAYBOOKS_DIR = '/etc/ansible/playbooks'
INVENTORY_FILE = '/etc/ansible/hosts'
ANSIBLE_CFG = '/etc/ansible/ansible.cfg'

os.makedirs(PLAYBOOKS_DIR, exist_ok=True)

def get_db():
    try:
        return mysql.connector.connect(**db_config)
    except Exception as e:
        print(f"❌ Erro MySQL: {e}")
        return None

@app.route('/')
def serve_frontend():
    return send_file('index.html')

# --- AUTH & USERS ---
@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute('SELECT * FROM users WHERE username = %s AND password = %s', (d.get('user'), d.get('password')))
    user = cur.fetchone()
    conn.close()
    if user: return jsonify({"status": "success", "user": {"name": user['username'], "role": user['role']}})
    return jsonify({"status": "error", "message": "Credenciais inválidas"}), 401

@app.route('/api/register', methods=['POST'])
def register():
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", (d.get('user'), d.get('password'), 'User'))
        conn.commit()
        return jsonify({"status": "success", "message": "Conta criada!"})
    except: return jsonify({"status": "error", "message": "Erro ao criar."}), 500
    finally: conn.close()

@app.route('/api/users', methods=['GET'])
def list_users():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, username, role FROM users")
    res = cur.fetchall()
    conn.close()
    return jsonify(res)

@app.route('/api/users/promote', methods=['POST'])
def promote_user():
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = %s WHERE id = %s", (d.get('role'), d.get('id')))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

# --- INVENTÁRIO (FONTE DA VERDADE = ARQUIVO FÍSICO) ---
@app.route('/api/inventory', methods=['GET', 'POST'])
def handle_inventory():
    if request.method == 'POST':
        d = request.json
        hostname = d.get('hostname')
        ip = d.get('ip')
        group = d.get('group', 'all')
        try:
            with open(INVENTORY_FILE, 'a') as f:
                f.write(f"\n[{group}]\n{hostname} ansible_host={ip}\n")
            return jsonify({"status": "success", "message": "Host salvo!"})
        except PermissionError:
            return jsonify({"status": "error", "message": "Sem permissão no arquivo hosts."}), 403

    hosts_list = []
    current_group = "all"
    try:
        with open(INVENTORY_FILE, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith('#') or stripped.startswith(';'): continue
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_group = stripped[1:-1]
                else:
                    parts = stripped.split()
                    h_name = parts[0]
                    h_ip = "Desconhecido"
                    for p in parts[1:]:
                        if p.startswith('ansible_host='): h_ip = p.split('=')[1]
                    hosts_list.append({"hostname": h_name, "ip": h_ip, "group": current_group})
        return jsonify(hosts_list)
    except FileNotFoundError: return jsonify([])

@app.route('/api/inventory/<hostname>', methods=['DELETE'])
def delete_inventory(hostname):
    try:
        with open(INVENTORY_FILE, 'r') as f: lines = f.readlines()
        with open(INVENTORY_FILE, 'w') as f:
            for line in lines:
                first_word = line.strip().split()[0] if line.strip() else ""
                if first_word != hostname: f.write(line)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 403

@app.route('/api/inventory/file', methods=['GET', 'POST'])
def raw_inventory_file():
    if request.method == 'GET':
        try:
            with open(INVENTORY_FILE, 'r') as f: return jsonify({"content": f.read()})
        except: return jsonify({"content": ""})
    else:
        try:
            with open(INVENTORY_FILE, 'w') as f: f.write(request.json['content'])
            return jsonify({"status": "success", "message": "Arquivo atualizado!"})
        except: return jsonify({"status": "error", "message": "Sem permissão!"}), 403

# --- PLAYBOOKS ---
@app.route('/api/playbooks', methods=['GET'])
def list_playbooks():
    files = []
    for f in os.listdir(PLAYBOOKS_DIR):
        if f.endswith('.yml') or f.endswith('.yaml'):
            with open(os.path.join(PLAYBOOKS_DIR, f), 'r') as file:
                files.append({"id": f, "name": f, "content": file.read()})
    return jsonify(files)

@app.route('/api/playbooks', methods=['POST'])
def save_playbook():
    d = request.json
    name = d['name']
    if not name.endswith('.yml'): name += '.yml'
    path = os.path.join(PLAYBOOKS_DIR, name)
    try:
        with open(path, 'w') as f: f.write(d['content'])
        return jsonify({"status": "success"})
    except PermissionError:
        return jsonify({"status": "error", "message": "Sem permissão na pasta playbooks."}), 403

@app.route('/api/playbooks/<path:filename>', methods=['DELETE'])
def delete_playbook(filename):
    path = os.path.join(PLAYBOOKS_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 404

# --- AGENDAMENTOS & HISTÓRICO ---
@app.route('/api/schedules', methods=['GET', 'POST'])
def handle_schedules():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    if request.method == 'POST':
        d = request.json
        cur.execute("INSERT INTO schedules (playbook_name, cron_expression, description) VALUES (%s, %s, %s)", (d['playbook'], d['cron'], d['desc']))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    cur.execute("SELECT * FROM schedules")
    res = cur.fetchall()
    conn.close()
    return jsonify(res)

@app.route('/api/schedules/<int:id>', methods=['DELETE'])
def delete_schedule(id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM schedules WHERE id = %s", (id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/schedules/<int:id>/toggle', methods=['POST'])
def toggle_schedule(id):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT active FROM schedules WHERE id = %s", (id,))
    s = cur.fetchone()
    cur.execute("UPDATE schedules SET active = %s WHERE id = %s", (not s['active'], id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/history', methods=['GET'])
def get_history():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM history ORDER BY executed_at DESC LIMIT 20")
    res = cur.fetchall()
    conn.close()
    return jsonify(res)

# --- EXECUÇÃO REAL COM HOST ESPECÍFICO (--limit) ---
@app.route('/api/run-stream', methods=['GET'])
def run_stream():
    pb_name = request.args.get('playbook')
    user = request.args.get('user')
    limit_target = request.args.get('limit', '')
    
    def generate():
        playbook_path = os.path.join(PLAYBOOKS_DIR, pb_name)
        yield json.dumps({"t": f"🚀 INICIANDO ORQUESTRAÇÃO: {pb_name}", "c": "text-primary font-bold"}) + "\n"
        
        if not os.path.exists(playbook_path):
            yield json.dumps({"t": f"❌ Erro: Arquivo {playbook_path} não encontrado.", "c": "text-red-500 font-bold"}) + "\n"
            return

        cmd = ['ansible-playbook', '-i', INVENTORY_FILE, playbook_path]
        
        if limit_target.strip():
            cmd.extend(['--limit', limit_target.strip()])
            yield json.dumps({"t": f"🎯 ALVO RESTRITO A: {limit_target}", "c": "text-yellow-400 font-bold uppercase tracking-widest mt-2 mb-2"}) + "\n"

        env = os.environ.copy()
        env['ANSIBLE_CONFIG'] = ANSIBLE_CFG
        env['ANSIBLE_NOCOLOR'] = '1'
        env['PYTHONUNBUFFERED'] = '1'
        env['ANSIBLE_HOST_KEY_CHECKING'] = 'False'

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)

        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if not line: continue
            
            c = "text-slate-300"
            if "ok:" in line or "ok=" in line: c = "text-green-500 font-bold"
            elif "changed:" in line or "changed=" in line: c = "text-yellow-500 font-bold"
            elif "fatal:" in line or "failed=" in line or "unreachable=" in line: c = "text-red-500 font-bold"
            elif line.startswith("TASK") or line.startswith("PLAY"): c = "text-blue-400 font-bold mt-4"

            yield json.dumps({"t": line, "c": c}) + "\n"

        process.wait()
        status = 'Sucesso' if process.returncode == 0 else 'Falha'
        yield json.dumps({"t": f"// Finalizado (Status: {process.returncode})", "c": "text-slate-500 italic mt-2"}) + "\n"

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO history (user_name, playbook_name, status) VALUES (%s, %s, %s)", (user, pb_name, status))
        conn.commit()
        conn.close()

    return Response(generate(), mimetype='application/x-ndjson')

if __name__ == '__main__':
    print(f"🚀 Servidor Web ativo na porta 5000!")
    app.run(host='0.0.0.0', debug=True, port=5000)