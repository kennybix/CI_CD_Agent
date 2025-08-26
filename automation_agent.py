import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import json
import time
import re
import os
import subprocess
from datetime import datetime
from pathlib import Path
import requests
from typing import List, Dict, Optional, Tuple, Any
import base64
from dotenv import load_dotenv
import anthropic

# Load environment variables
load_dotenv()

class ClaudeCoordinator:
    """Claude AI coordinator for the build automation process"""
    
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.conversation_history = []
        
    def analyze_code_requirements(self, files: Dict[str, str]) -> Dict:
        """Use Claude to analyze C++ code and determine requirements"""
        
        # Prepare code context for Claude
        code_context = "I have the following C++ project files:\n\n"
        for filename, content in files.items():
            # Limit content size for API limits
            truncated = content[:2000] if len(content) > 2000 else content
            code_context += f"=== {filename} ===\n{truncated}\n...\n\n"
        
        prompt = f"""{code_context}

Please analyze these C++ files and provide:
1. All required dependencies (libraries) for vcpkg.json
2. The minimum C++ standard required
3. Any special build requirements or flags
4. List of all source files that should be compiled

Respond in JSON format:
{{
    "dependencies": ["lib1", "lib2"],
    "cpp_standard": "17",
    "source_files": ["main.cpp", "other.cpp"],
    "special_requirements": "any special notes",
    "cmake_flags": []
}}"""

        try:
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            
            # Extract JSON from response
            json_str = self._extract_json(response.content[0].text)
            return json.loads(json_str)
        except Exception as e:
            print(f"Claude analysis error: {e}")
            # Fallback to basic analysis
            return self._fallback_analysis(files)
    
    def generate_build_files(self, project_name: str, analysis: Dict, target_os: List[str]) -> Dict:
        """Use Claude to generate optimal build files"""
        
        prompt = f"""Generate build files for a C++ project with these requirements:
        
Project Name: {project_name}
Dependencies: {json.dumps(analysis.get('dependencies', []))}
C++ Standard: {analysis.get('cpp_standard', '17')}
Source Files: {json.dumps(analysis.get('source_files', []))}
Target OS: {', '.join(target_os)}
Special Requirements: {analysis.get('special_requirements', 'None')}

Please generate:
1. vcpkg.json
2. CMakeLists.txt  
3. GitHub Actions workflow (.github/workflows/build.yml)

The workflow should build for: {', '.join(target_os)}

Respond with JSON containing the content of each file:
{{
    "vcpkg.json": "content",
    "CMakeLists.txt": "content",
    "workflow.yml": "content"
}}"""

        try:
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )
            
            json_str = self._extract_json(response.content[0].text)
            return json.loads(json_str)
        except Exception as e:
            print(f"Claude generation error: {e}")
            return self._fallback_generation(project_name, analysis, target_os)
    
    def fix_build_errors(self, error_log: str, current_files: Dict[str, str], 
                        attempt: int, source_files: Dict[str, str] = None) -> Dict:
        """Use Claude to analyze build errors and suggest fixes"""
        
        # Include source file snippets if we're on later attempts
        source_context = ""
        if attempt >= 3 and source_files:
            # Find files mentioned in errors
            error_files = re.findall(r'([a-zA-Z0-9_/]+\.\w+):\d+:\d+:', error_log)
            for error_file in set(error_files[:3]):  # Limit to 3 files
                if error_file in source_files:
                    source_context += f"\n=== {error_file} (snippet) ===\n"
                    source_context += source_files[error_file][:1000] + "\n...\n"
        
        prompt = f"""Build attempt {attempt} failed with these errors:

{error_log[:3000]}  # Truncate for API limits

Current build files:
vcpkg.json: {current_files.get('vcpkg.json', 'N/A')[:500]}
CMakeLists.txt: {current_files.get('CMakeLists.txt', 'N/A')[:1000]}
{source_context}

Please analyze the errors and provide fixes. 

IMPORTANT: Follow this priority order:
1. First 2 attempts: ONLY modify build configuration files (vcpkg.json, CMakeLists.txt, workflow)
2. Attempt 3-4: Consider minimal code changes only if build config won't work
3. Final attempt: Apply necessary code changes to fix compilation errors

For code changes, provide the EXACT changes needed with clear before/after.

Respond with JSON:
{{
    "diagnosis": "what went wrong",
    "vcpkg_changes": "new vcpkg.json content or null",
    "cmake_changes": "new CMakeLists.txt content or null",
    "workflow_changes": "new workflow content or null",
    "code_changes": {{
        "filename": {{
            "action": "replace|add|remove",
            "find": "exact text to find",
            "replace": "exact replacement text",
            "line_number": optional_line_number,
            "explanation": "why this change is needed"
        }}
    }},
    "confidence": 0.0 to 1.0,
    "requires_code_change": true/false
}}"""

        try:
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4000,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            json_str = self._extract_json(response.content[0].text)
            return json.loads(json_str)
        except Exception as e:
            print(f"Claude error fix failed: {e}")
            return {"diagnosis": "Failed to analyze", "confidence": 0.0}
    
    def _extract_json(self, text: str) -> str:
        """Extract JSON from Claude's response"""
        # Try to find JSON between ```json and ``` or just ```
        import re
        
        # Pattern 1: ```json ... ```
        pattern1 = r'```json\s*(.*?)\s*```'
        match = re.search(pattern1, text, re.DOTALL)
        if match:
            return match.group(1)
        
        # Pattern 2: ``` ... ```  
        pattern2 = r'```\s*(.*?)\s*```'
        match = re.search(pattern2, text, re.DOTALL)
        if match:
            return match.group(1)
        
        # Pattern 3: Raw JSON
        pattern3 = r'\{.*\}'
        match = re.search(pattern3, text, re.DOTALL)
        if match:
            return match.group(0)
        
        return text
    
    def _fallback_analysis(self, files: Dict[str, str]) -> Dict:
        """Fallback analysis if Claude fails"""
        dependencies = set()
        source_files = []
        
        for filename, content in files.items():
            if filename.endswith(('.cpp', '.cc')):
                source_files.append(filename)
            
            # Basic dependency detection
            if '#include <boost' in content:
                dependencies.add('boost')
            if '#include <openssl' in content:
                dependencies.add('openssl')
            if '#include <curl' in content:
                dependencies.add('curl')
            if 'nlohmann/json' in content:
                dependencies.add('nlohmann-json')
        
        return {
            'dependencies': list(dependencies),
            'cpp_standard': '17',
            'source_files': source_files,
            'special_requirements': ''
        }
    
    def _fallback_generation(self, project_name: str, analysis: Dict, target_os: List[str]) -> Dict:
        """Fallback generation if Claude fails"""
        # Basic templates
        vcpkg = {
            "name": project_name.lower().replace(" ", "-"),
            "version": "1.0.0",
            "dependencies": analysis.get('dependencies', [])
        }
        
        cmake = f"""cmake_minimum_required(VERSION 3.16)
project({project_name})

set(CMAKE_CXX_STANDARD {analysis.get('cpp_standard', '17')})
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable({project_name} {' '.join(analysis.get('source_files', []))})
"""
        
        workflow = f"""name: Build
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    - name: Build
      run: |
        mkdir build && cd build
        cmake .. && make
"""
        
        return {
            'vcpkg.json': json.dumps(vcpkg, indent=2),
            'CMakeLists.txt': cmake,
            'workflow.yml': workflow
        }

class GitHubAPI:
    """GitHub API client for repository operations"""
    
    def __init__(self, token: str, repo_url: str):
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        # Parse repo URL
        parts = repo_url.replace("https://github.com/", "").replace(".git", "").split("/")
        self.owner = parts[0]
        self.repo = parts[1]
        self.base_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"
    
    def get_file(self, path: str) -> Optional[str]:
        """Get file content from repository"""
        url = f"{self.base_url}/contents/{path}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            content = response.json()['content']
            return base64.b64decode(content).decode('utf-8')
        return None
    
    def create_or_update_file(self, path: str, content: str, message: str) -> bool:
        """Create or update file in repository"""
        url = f"{self.base_url}/contents/{path}"
        
        # Check if file exists
        response = requests.get(url, headers=self.headers)
        sha = response.json().get('sha') if response.status_code == 200 else None
        
        data = {
            'message': message,
            'content': base64.b64encode(content.encode()).decode(),
        }
        if sha:
            data['sha'] = sha
        
        response = requests.put(url, headers=self.headers, json=data)
        return response.status_code in [200, 201]
    
    def list_files(self, path: str = "") -> List[Dict]:
        """List files in repository"""
        url = f"{self.base_url}/contents/{path}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            return response.json()
        return []
    
    def get_workflow_runs(self) -> List[Dict]:
        """Get recent workflow runs"""
        url = f"{self.base_url}/actions/runs"
        response = requests.get(url, headers=self.headers, params={'per_page': 5})
        if response.status_code == 200:
            return response.json().get('workflow_runs', [])
        return []
    
    def get_run_logs(self, run_id: int) -> str:
        """Get logs for a workflow run"""
        url = f"{self.base_url}/actions/runs/{run_id}/logs"
        response = requests.get(url, headers=self.headers, allow_redirects=True)
        if response.status_code == 200:
            return response.text
        return ""
    
    def get_run_status(self, run_id: int) -> Dict:
        """Get status of a workflow run"""
        url = f"{self.base_url}/actions/runs/{run_id}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            data = response.json()
            return {
                'status': data.get('status'),
                'conclusion': data.get('conclusion'),
                'url': data.get('html_url')
            }
        return {}

class ConfigManager:
    """Manages configuration from config.json"""
    
    @staticmethod
    def load_config() -> Dict:
        """Load configuration from config.json"""
        config_path = Path("config.json")
        if config_path.exists():
            with open(config_path, 'r') as f:
                return json.load(f)
        else:
            # Create default config
            default_config = {
                "target_os": ["ubuntu", "windows", "macos"],
                "max_fix_attempts": 5,
                "github_timeout": 300,
                "auto_commit": True,
                "verbose_logging": True,
                "claude_model": "claude-3-5-sonnet-20241022"
            }
            ConfigManager.save_config(default_config)
            return default_config
    
    @staticmethod
    def save_config(config: Dict):
        """Save configuration to config.json"""
        with open("config.json", 'w') as f:
            json.dump(config, f, indent=2)

class BuildAutomationGUI:
    """Main GUI application with Claude coordination"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("C++ Build Automation Platform - Claude Powered")
        self.root.geometry("1400x900")
        
        # Load configuration
        self.config = ConfigManager.load_config()
        
        # Load environment variables
        self.env_vars = {
            'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY', ''),
            'GITHUB_TOKEN': os.getenv('GITHUB_TOKEN', '')
        }
        
        # Check for .env file
        if not self.env_vars['ANTHROPIC_API_KEY']:
            self.create_env_file()
        
        # Variables
        self.repo_url = tk.StringVar()
        self.project_name = tk.StringVar(value="MyProject")
        self.automation_running = False
        
        # Services
        self.claude = None
        self.github = None
        
        self.setup_ui()
    
    def create_env_file(self):
        """Create .env file if it doesn't exist"""
        if not Path('.env').exists():
            with open('.env', 'w') as f:
                f.write("# API Keys for C++ Build Automation\n")
                f.write("ANTHROPIC_API_KEY=your_claude_api_key_here\n")
                f.write("GITHUB_TOKEN=your_github_token_here\n")
            messagebox.showinfo("Setup Required", 
                              ".env file created. Please add your API keys and restart.")
    
    def setup_ui(self):
        """Setup the GUI interface"""
        # Create notebook for tabs
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill='both', expand=True, padx=10, pady=10)
        
        # Main tab
        main_tab = ttk.Frame(notebook)
        notebook.add(main_tab, text="Build Automation")
        
        # Configuration tab
        config_tab = ttk.Frame(notebook)
        notebook.add(config_tab, text="Configuration")
        
        # Setup main tab
        self.setup_main_tab(main_tab)
        
        # Setup config tab
        self.setup_config_tab(config_tab)
    
    def setup_main_tab(self, parent):
        """Setup main automation tab"""
        # Repository Section
        repo_frame = ttk.LabelFrame(parent, text="Repository Configuration", padding="10")
        repo_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(repo_frame, text="Repository URL:").grid(row=0, column=0, sticky='w')
        repo_entry = ttk.Entry(repo_frame, textvariable=self.repo_url, width=60)
        repo_entry.grid(row=0, column=1, padx=5)
        
        ttk.Label(repo_frame, text="Project Name:").grid(row=1, column=0, sticky='w')
        name_entry = ttk.Entry(repo_frame, textvariable=self.project_name, width=60)
        name_entry.grid(row=1, column=1, padx=5)
        
        # API Status
        status_frame = ttk.LabelFrame(parent, text="API Status", padding="10")
        status_frame.pack(fill='x', padx=10, pady=5)
        
        self.api_status_labels = {}
        for i, (key, value) in enumerate([
            ("Claude API", "✓" if self.env_vars['ANTHROPIC_API_KEY'] else "✗"),
            ("GitHub API", "✓" if self.env_vars['GITHUB_TOKEN'] else "✗")
        ]):
            ttk.Label(status_frame, text=f"{key}:").grid(row=0, column=i*2, padx=5)
            label = ttk.Label(status_frame, text=value, 
                            foreground="green" if value == "✓" else "red")
            label.grid(row=0, column=i*2+1, padx=5)
            self.api_status_labels[key] = label
        
        # Control Section
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill='x', padx=10, pady=10)
        
        self.start_button = ttk.Button(control_frame, text="🚀 Start Claude Automation",
                                      command=self.start_automation,
                                      style="Accent.TButton")
        self.start_button.pack(side='left', padx=5)
        
        self.stop_button = ttk.Button(control_frame, text="⏹ Stop",
                                     command=self.stop_automation,
                                     state='disabled')
        self.stop_button.pack(side='left', padx=5)
        
        ttk.Button(control_frame, text="📋 Clear Log",
                  command=self.clear_log).pack(side='left', padx=5)
        
        ttk.Button(control_frame, text="💾 Export Log",
                  command=self.export_log).pack(side='left', padx=5)
        
        # Progress Section
        progress_frame = ttk.Frame(parent)
        progress_frame.pack(fill='x', padx=10, pady=5)
        
        self.progress = ttk.Progressbar(progress_frame, mode='indeterminate')
        self.progress.pack(fill='x', pady=2)
        
        self.status_label = ttk.Label(progress_frame, text="Ready", font=('Arial', 10, 'bold'))
        self.status_label.pack()
        
        # Claude Conversation Section
        claude_frame = ttk.LabelFrame(parent, text="Claude Coordinator", padding="10")
        claude_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Create two panes - one for Claude's thinking, one for logs
        paned = ttk.PanedWindow(claude_frame, orient='horizontal')
        paned.pack(fill='both', expand=True)
        
        # Claude thinking pane
        thinking_frame = ttk.LabelFrame(paned, text="Claude's Analysis", padding="5")
        self.claude_text = scrolledtext.ScrolledText(thinking_frame, height=15, width=50,
                                                     wrap=tk.WORD, bg='#f0f8ff')
        self.claude_text.pack(fill='both', expand=True)
        paned.add(thinking_frame)
        
        # Build log pane
        log_frame = ttk.LabelFrame(paned, text="Build Log", padding="5")
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=50,
                                                  wrap=tk.WORD)
        self.log_text.pack(fill='both', expand=True)
        paned.add(log_frame)
    
    def setup_config_tab(self, parent):
        """Setup configuration tab"""
        # OS Selection
        os_frame = ttk.LabelFrame(parent, text="Target Operating Systems", padding="10")
        os_frame.pack(fill='x', padx=10, pady=10)
        
        self.os_vars = {}
        for i, os_name in enumerate(['ubuntu', 'windows', 'macos']):
            var = tk.BooleanVar(value=os_name in self.config.get('target_os', []))
            self.os_vars[os_name] = var
            ttk.Checkbutton(os_frame, text=os_name.capitalize(),
                          variable=var,
                          command=self.update_config).grid(row=0, column=i, padx=10)
        
        # Build Settings
        settings_frame = ttk.LabelFrame(parent, text="Build Settings", padding="10")
        settings_frame.pack(fill='x', padx=10, pady=10)
        
        ttk.Label(settings_frame, text="Max Fix Attempts:").grid(row=0, column=0, sticky='w')
        self.max_attempts_var = tk.IntVar(value=self.config.get('max_fix_attempts', 5))
        ttk.Spinbox(settings_frame, from_=1, to=10, textvariable=self.max_attempts_var,
                   width=10, command=self.update_config).grid(row=0, column=1, padx=5)
        
        ttk.Label(settings_frame, text="GitHub Timeout (seconds):").grid(row=1, column=0, sticky='w')
        self.timeout_var = tk.IntVar(value=self.config.get('github_timeout', 300))
        ttk.Spinbox(settings_frame, from_=60, to=600, increment=30,
                   textvariable=self.timeout_var,
                   width=10, command=self.update_config).grid(row=1, column=1, padx=5)
        
        self.auto_commit_var = tk.BooleanVar(value=self.config.get('auto_commit', True))
        ttk.Checkbutton(settings_frame, text="Auto-commit fixes",
                       variable=self.auto_commit_var,
                       command=self.update_config).grid(row=2, column=0, columnspan=2, pady=5)
        
        self.verbose_var = tk.BooleanVar(value=self.config.get('verbose_logging', True))
        ttk.Checkbutton(settings_frame, text="Verbose logging",
                       variable=self.verbose_var,
                       command=self.update_config).grid(row=3, column=0, columnspan=2, pady=5)
        
        # API Keys Section
        api_frame = ttk.LabelFrame(parent, text="API Configuration", padding="10")
        api_frame.pack(fill='x', padx=10, pady=10)
        
        ttk.Label(api_frame, text="Edit .env file to update API keys").pack()
        ttk.Button(api_frame, text="Open .env file",
                  command=self.open_env_file).pack(pady=5)
        ttk.Button(api_frame, text="Reload Environment",
                  command=self.reload_env).pack(pady=5)
    
    def update_config(self):
        """Update configuration when settings change"""
        self.config['target_os'] = [os_name for os_name, var in self.os_vars.items() if var.get()]
        self.config['max_fix_attempts'] = self.max_attempts_var.get()
        self.config['github_timeout'] = self.timeout_var.get()
        self.config['auto_commit'] = self.auto_commit_var.get()
        self.config['verbose_logging'] = self.verbose_var.get()
        ConfigManager.save_config(self.config)
    
    def open_env_file(self):
        """Open .env file in default editor"""
        import platform
        env_path = Path('.env')
        if env_path.exists():
            if platform.system() == 'Windows':
                os.startfile(env_path)
            elif platform.system() == 'Darwin':  # macOS
                subprocess.run(['open', env_path])
            else:  # Linux
                subprocess.run(['xdg-open', env_path])
    
    def reload_env(self):
        """Reload environment variables"""
        load_dotenv(override=True)
        self.env_vars = {
            'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY', ''),
            'GITHUB_TOKEN': os.getenv('GITHUB_TOKEN', '')
        }
        
        # Update status labels
        self.api_status_labels["Claude API"].config(
            text="✓" if self.env_vars['ANTHROPIC_API_KEY'] else "✗",
            foreground="green" if self.env_vars['ANTHROPIC_API_KEY'] else "red"
        )
        self.api_status_labels["GitHub API"].config(
            text="✓" if self.env_vars['GITHUB_TOKEN'] else "✗",
            foreground="green" if self.env_vars['GITHUB_TOKEN'] else "red"
        )
        
        messagebox.showinfo("Environment Reloaded", "API keys have been reloaded from .env")
    
    def log(self, message: str, level: str = "INFO"):
        """Add message to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}\n"
        self.log_text.insert(tk.END, log_entry)
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def claude_log(self, message: str):
        """Add message to Claude's thinking pane"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] 🤖 {message}\n"
        self.claude_text.insert(tk.END, log_entry)
        self.claude_text.see(tk.END)
        self.root.update_idletasks()
    
    def clear_log(self):
        """Clear both log outputs"""
        self.log_text.delete(1.0, tk.END)
        self.claude_text.delete(1.0, tk.END)
    
    def export_log(self):
        """Export logs to file"""
        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if filename:
            with open(filename, 'w') as f:
                f.write("=== Claude Analysis ===\n")
                f.write(self.claude_text.get(1.0, tk.END))
                f.write("\n=== Build Log ===\n")
                f.write(self.log_text.get(1.0, tk.END))
            messagebox.showinfo("Export Complete", f"Logs exported to {filename}")
    
    def update_status(self, message: str, color: str = "black"):
        """Update status label"""
        self.status_label.config(text=message, foreground=color)
        self.root.update_idletasks()
    
    def start_automation(self):
        """Start the Claude-coordinated automation"""
        # Validate inputs
        if not self.env_vars['ANTHROPIC_API_KEY']:
            messagebox.showerror("Error", "Please add ANTHROPIC_API_KEY to .env file")
            return
        
        if not self.env_vars['GITHUB_TOKEN']:
            messagebox.showerror("Error", "Please add GITHUB_TOKEN to .env file")
            return
        
        if not self.repo_url.get():
            messagebox.showerror("Error", "Please provide repository URL")
            return
        
        # Initialize services
        self.claude = ClaudeCoordinator(self.env_vars['ANTHROPIC_API_KEY'])
        self.github = GitHubAPI(self.env_vars['GITHUB_TOKEN'], self.repo_url.get())
        
        # Start automation
        self.automation_running = True
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')
        self.progress.start()
        
        # Run in thread
        thread = threading.Thread(target=self.run_claude_automation)
        thread.daemon = True
        thread.start()
    
    def stop_automation(self):
        """Stop the automation"""
        self.automation_running = False
        self.start_button.config(state='normal')
        self.stop_button.config(state='disabled')
        self.progress.stop()
        self.update_status("Stopped", "red")
        self.log("Automation stopped by user", "WARNING")
    
    def run_claude_automation(self):
        """Main automation loop coordinated by Claude"""
        try:
            self.update_status("Claude is analyzing your project...", "blue")
            self.log("Starting Claude-coordinated build automation")
            self.claude_log("Initializing analysis of C++ project...")
            
            # Step 1: Fetch code files
            self.log("Step 1: Fetching repository files...")
            source_files = self.fetch_code_files()
            if not source_files:
                self.log("No C++ files found", "ERROR")
                return
            
            self.log(f"Found {len(source_files)} code files")
            self.claude_log(f"Analyzing {len(source_files)} C++ files...")
            
            # Keep track of original files for potential modifications
            self.original_source_files = source_files.copy()
            
            # Step 2: Claude analyzes requirements
            self.log("Step 2: Claude is analyzing code requirements...")
            analysis = self.claude.analyze_code_requirements(source_files)
            
            self.claude_log(f"Detected dependencies: {', '.join(analysis.get('dependencies', []))}")
            self.claude_log(f"C++ Standard: {analysis.get('cpp_standard', '17')}")
            self.claude_log(f"Special requirements: {analysis.get('special_requirements', 'None')}")
            
            # Step 3: Claude generates build files
            self.log("Step 3: Claude is generating optimized build files...")
            self.claude_log("Generating vcpkg.json, CMakeLists.txt, and GitHub Actions workflow...")
            
            build_files = self.claude.generate_build_files(
                self.project_name.get(),
                analysis,
                self.config['target_os']
            )
            
            # Step 4: Push files to repository
            self.log("Step 4: Pushing build files to repository...")
            current_files = {}
            
            for filename, content in [
                ('vcpkg.json', build_files.get('vcpkg.json')),
                ('CMakeLists.txt', build_files.get('CMakeLists.txt')),
                ('.github/workflows/build.yml', build_files.get('workflow.yml'))
            ]:
                if content:
                    if self.github.create_or_update_file(filename, content,
                                                        f"Claude: Add/Update {filename}"):
                        self.log(f"Created {filename}")
                        current_files[filename] = content
                    else:
                        self.log(f"Failed to create {filename}", "ERROR")
            
            self.claude_log("Build files created and pushed to repository")
            
            # Step 5: Monitor builds and let Claude fix errors
            self.log("Step 5: Monitoring builds and applying Claude's fixes...")
            self.monitor_and_fix_with_claude(current_files)
            
        except Exception as e:
            self.log(f"Automation failed: {str(e)}", "ERROR")
            self.claude_log(f"Error encountered: {str(e)}")
            self.update_status("Failed", "red")
        finally:
            self.automation_running = False
            self.start_button.config(state='normal')
            self.stop_button.config(state='disabled')
            self.progress.stop()
    
    def fetch_code_files(self) -> Dict[str, str]:
        """Fetch all C++ related files from repository"""
        files = {}
        
        def fetch_recursive(path=""):
            items = self.github.list_files(path)
            for item in items:
                if not self.automation_running:
                    break
                    
                if item['type'] == 'file':
                    if any(item['name'].endswith(ext) for ext in ['.cpp', '.h', '.hpp', '.cc', '.c']):
                        content = self.github.get_file(item['path'])
                        if content:
                            files[item['path']] = content
                            self.log(f"Fetched: {item['path']}")
                elif item['type'] == 'dir' and not item['name'].startswith('.'):
                    fetch_recursive(item['path'])
        
        fetch_recursive()
        return files
    
    def monitor_and_fix_with_claude(self, current_files: Dict[str, str]):
        """Monitor builds and let Claude fix any errors"""
        max_attempts = self.config.get('max_fix_attempts', 5)
        attempt = 0
        
        while attempt < max_attempts and self.automation_running:
            attempt += 1
            self.log(f"Build attempt {attempt}/{max_attempts}")
            self.claude_log(f"Monitoring build attempt {attempt}...")
            
            # Wait for workflow to start
            self.update_status(f"Waiting for GitHub Actions (attempt {attempt})...", "blue")
            time.sleep(15)
            
            # Get latest workflow run
            runs = self.github.get_workflow_runs()
            if not runs:
                self.log("No workflow runs found, waiting...", "WARNING")
                time.sleep(10)
                continue
            
            latest_run = runs[0]
            run_id = latest_run['id']
            run_url = latest_run.get('html_url', '')
            
            self.log(f"Monitoring workflow run: {run_id}")
            if run_url:
                self.log(f"View on GitHub: {run_url}")
            
            # Wait for workflow completion
            start_time = time.time()
            timeout = self.config.get('github_timeout', 300)
            
            while self.automation_running:
                if time.time() - start_time > timeout:
                    self.log("Build timeout reached", "WARNING")
                    break
                
                status = self.github.get_run_status(run_id)
                if status.get('status') == 'completed':
                    break
                
                self.update_status(f"Build running... ({int(time.time() - start_time)}s)", "blue")
                time.sleep(10)
            
            if not self.automation_running:
                break
            
            # Check build result
            status = self.github.get_run_status(run_id)
            
            if status.get('conclusion') == 'success':
                self.log("✅ Build succeeded!", "SUCCESS")
                self.claude_log("Build successful! All targets compiled successfully.")
                self.update_status("Build successful!", "green")
                
                # Show success details
                success_msg = f"""
Build Automation Complete!
━━━━━━━━━━━━━━━━━━━━━
✅ All builds passed
📦 Artifacts available on GitHub
🎯 Target OS: {', '.join(self.config['target_os'])}
⏱️ Attempts needed: {attempt}
━━━━━━━━━━━━━━━━━━━━━
"""
                self.claude_log(success_msg)
                break
                
            elif status.get('conclusion') in ['failure', 'cancelled']:
                self.log(f"Build failed. Claude is analyzing errors...", "WARNING")
                self.claude_log(f"Build failed. Analyzing error logs...")
                
                # Get error logs
                error_log = self.github.get_run_logs(run_id)
                if not error_log:
                    self.log("Could not retrieve error logs", "ERROR")
                    continue
                
                # Let Claude analyze and fix
                self.claude_log("Diagnosing build errors and generating fixes...")
                fixes = self.claude.fix_build_errors(error_log, current_files, attempt)
                
                if fixes.get('confidence', 0) < 0.3:
                    self.log("Claude has low confidence in fixes", "WARNING")
                    self.claude_log(f"Diagnosis: {fixes.get('diagnosis', 'Unknown')}")
                    self.claude_log("Manual intervention may be required")
                    
                    if attempt >= max_attempts - 1:
                        break
                
                # Apply Claude's fixes
                self.log("Applying Claude's fixes...")
                self.claude_log(f"Applying fixes: {fixes.get('diagnosis', '')}")
                
                files_updated = False
                
                # Update vcpkg.json if needed
                if fixes.get('vcpkg_changes'):
                    self.log("Updating vcpkg.json...")
                    if self.github.create_or_update_file(
                        'vcpkg.json',
                        fixes['vcpkg_changes'],
                        f"Claude fix {attempt}: Update dependencies"
                    ):
                        current_files['vcpkg.json'] = fixes['vcpkg_changes']
                        files_updated = True
                
                # Update CMakeLists.txt if needed
                if fixes.get('cmake_changes'):
                    self.log("Updating CMakeLists.txt...")
                    if self.github.create_or_update_file(
                        'CMakeLists.txt',
                        fixes['cmake_changes'],
                        f"Claude fix {attempt}: Update CMake configuration"
                    ):
                        current_files['CMakeLists.txt'] = fixes['cmake_changes']
                        files_updated = True
                
                # Update workflow if needed
                if fixes.get('workflow_changes'):
                    self.log("Updating workflow...")
                    if self.github.create_or_update_file(
                        '.github/workflows/build.yml',
                        fixes['workflow_changes'],
                        f"Claude fix {attempt}: Update workflow"
                    ):
                        current_files['workflow.yml'] = fixes['workflow_changes']
                        files_updated = True
                
                # Apply code changes if absolutely necessary
                if fixes.get('code_changes') and attempt == max_attempts - 1:
                    self.log("Claude suggests code changes:", "WARNING")
                    for filename, changes in fixes['code_changes'].items():
                        self.claude_log(f"Code change needed in {filename}: {changes}")
                
                if not files_updated:
                    self.log("No fixes could be applied", "WARNING")
                    if attempt >= max_attempts - 1:
                        break
                
                # Wait before next attempt
                self.claude_log("Waiting for next build attempt...")
                time.sleep(5)
            
            else:
                self.log(f"Unknown build status: {status.get('conclusion')}", "WARNING")
        
        if attempt >= max_attempts:
            self.log(f"Max attempts ({max_attempts}) reached", "ERROR")
            self.claude_log("Maximum fix attempts reached. Manual intervention may be required.")
            self.update_status("Build failed - manual intervention needed", "red")
            
            # Provide final summary
            summary = f"""
Build Automation Summary
━━━━━━━━━━━━━━━━━━━━━
❌ Build did not succeed
📊 Attempts made: {attempt}/{max_attempts}
🔧 Fixes applied: Multiple
⚠️ Manual review recommended
━━━━━━━━━━━━━━━━━━━━━

Please check the GitHub Actions logs for detailed error information.
You may need to manually adjust the code or build configuration.
"""
            self.claude_log(summary)

def main():
    """Main entry point"""
    # Create default files if they don't exist
    if not Path('.env').exists():
        with open('.env', 'w') as f:
            f.write("# API Keys for C++ Build Automation\n")
            f.write("ANTHROPIC_API_KEY=your_claude_api_key_here\n")
            f.write("GITHUB_TOKEN=your_github_token_here\n")
        print("Created .env file. Please add your API keys.")
    
    if not Path('config.json').exists():
        ConfigManager.load_config()  # This creates default config
        print("Created config.json with default settings.")
    
    # Start GUI
    root = tk.Tk()
    
    # Set a nice style
    style = ttk.Style()
    style.theme_use('clam')  # or 'alt', 'default', 'classic'
    
    app = BuildAutomationGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()