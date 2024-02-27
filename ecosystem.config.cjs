module.exports = {
  apps: [
    {
      name: "subnet-agent",
      cwd: "/root/AI_agent/Subnet_agent",
      script: "/root/AI_agent/Subnet_agent/.venv/bin/python",
      args: "-m bot.main",
      interpreter: "none",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      env: {
        HOME: "/root",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
