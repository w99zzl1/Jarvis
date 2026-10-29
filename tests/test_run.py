#!/usr/bin/env python
import sys
sys.path.insert(0, '.')
from server.routes.agent import agent_route
import os

# Test 1: Simple file read
print('=== TEST 1: Read file ===')
result = agent_route(user_input='Прочитай файл ReadMe.md', api_keys=None, system_prompt='', primary_model='openai/gpt-oss-120b')
print('success:', result['success'], 'state:', result['state'])

# Test 2: Write a file  
print('=== TEST 2: Write file ===')
result = agent_route(user_input='Создай файл test_dummy.txt строка Hello', api_keys=None, system_prompt='', primary_model='openai/gpt-oss-120b')
print('success:', result['success'], 'state:', result['state'])
if os.path.exists('test_dummy.txt'):
    with open('test_dummy.txt') as f:
        print('file content:', f.read())
    os.remove('test_dummy.txt')

# Test 3: Edit file
with open('test_edit.txt', 'w') as f:
    f.write('Line1: original\nLine2: to change\n')
result = agent_route(user_input='Исправляя edit_file, замени original на modified в test_edit.txt', api_keys=None, system_prompt='', primary_model='openai/gpt-oss-120b')
print('success:', result['success'], 'state:', result['state'])
if os.path.exists('test_edit.txt'):
    with open('test_edit.txt') as f:
        print('file after:', f.read())
    os.remove('test_edit.txt')

# Test 4: Planning
result = agent_route(user_input='Создай план из 3 шагов для улучшения проекта', api_keys=None, system_prompt='', primary_model='openai/gpt-oss-120b')
print('success:', result['success'], 'state:', result['state'], 'iterations:', result.get('iterations'), 'tool_calls:', result.get('tool_calls_total'))