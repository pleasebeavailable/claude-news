.PHONY: start stop restart status logs install uninstall

PROJECT_DIR := $(shell pwd)
PID_FILE    := /tmp/claude-intel.pid
LOG_DIR     := $(PROJECT_DIR)/logs

start:
	@bash bin/start

stop:
	@if [ -f $(PID_FILE) ]; then \
		PID=$$(cat $(PID_FILE)); \
		if kill -0 $$PID 2>/dev/null; then \
			kill $$PID && rm -f $(PID_FILE) && echo "Stopped (PID=$$PID)"; \
		else \
			rm -f $(PID_FILE) && echo "Not running (stale PID file removed)"; \
		fi \
	else \
		echo "Not running"; \
	fi

restart: stop
	@sleep 1
	@bash bin/start

status:
	@if [ -f $(PID_FILE) ]; then \
		PID=$$(cat $(PID_FILE)); \
		if kill -0 $$PID 2>/dev/null; then \
			echo "Running (PID=$$PID)"; \
		else \
			echo "Not running (stale PID file)"; \
		fi \
	else \
		echo "Not running"; \
	fi
	@launchctl list | grep claude.intel || true

logs:
	@tail -f $(LOG_DIR)/app.log

install:
	@ln -sf $(PROJECT_DIR)/bin/com.claude.intel.plist ~/Library/LaunchAgents/com.claude.intel.plist
	@launchctl load ~/Library/LaunchAgents/com.claude.intel.plist
	@echo "Installed and started via launchd"

uninstall:
	@launchctl unload ~/Library/LaunchAgents/com.claude.intel.plist 2>/dev/null || true
	@rm -f ~/Library/LaunchAgents/com.claude.intel.plist
	@echo "Uninstalled"
