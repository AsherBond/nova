# Copyright 2014 NEC Corporation.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

get_vnc_console = {
    'type': 'object',
    'properties': {
        'os-getVNCConsole': {
            'type': 'object',
            'properties': {
                'type': {
                    'type': 'string',
                    'enum': ['novnc', 'xvpvnc'],
                },
            },
            'required': ['type'],
            'additionalProperties': False,
        },
    },
    'required': ['os-getVNCConsole'],
    'additionalProperties': False,
}

get_spice_console = {
    'type': 'object',
    'properties': {
        'os-getSPICEConsole': {
            'type': 'object',
            'properties': {
                'type': {
                    'type': 'string',
                    'enum': ['spice-html5'],
                },
            },
            'required': ['type'],
            'additionalProperties': False,
        },
    },
    'required': ['os-getSPICEConsole'],
    'additionalProperties': False,
}

# NOTE(stephenfin): This schema is intentionally empty since the action has
# been removed
get_rdp_console = {}

get_serial_console = {
    'type': 'object',
    'properties': {
        'os-getSerialConsole': {
            'type': 'object',
            'properties': {
                'type': {
                    'type': 'string',
                    'enum': ['serial'],
                },
            },
            'required': ['type'],
            'additionalProperties': False,
        },
    },
    'required': ['os-getSerialConsole'],
    'additionalProperties': False,
}

create_v26 = {
    'type': 'object',
    'properties': {
        'remote_console': {
            'type': 'object',
            'properties': {
                'protocol': {
                    'type': 'string',
                    'enum': ['vnc', 'spice', 'serial'],
                },
                'type': {
                    'type': 'string',
                    'enum': ['novnc', 'xvpvnc', 'spice-html5', 'serial'],
                },
            },
            'required': ['protocol', 'type'],
            'additionalProperties': False,
        },
    },
    'required': ['remote_console'],
    'additionalProperties': False,
}

create_v28 = {
    'type': 'object',
    'properties': {
        'remote_console': {
            'type': 'object',
            'properties': {
                'protocol': {
                    'type': 'string',
                    'enum': ['vnc', 'spice', 'serial', 'mks'],
                },
                'type': {
                    'type': 'string',
                    'enum': ['novnc', 'xvpvnc', 'spice-html5', 'serial',
                             'webmks'],
                },
            },
            'required': ['protocol', 'type'],
            'additionalProperties': False,
        },
    },
    'required': ['remote_console'],
    'additionalProperties': False,
}

create_v299 = {
    'type': 'object',
    'properties': {
        'remote_console': {
            'type': 'object',
            'properties': {
                'protocol': {
                    'type': 'string',
                    'enum': ['vnc', 'spice', 'rdp', 'serial', 'mks'],
                },
                'type': {
                    'type': 'string',
                    'enum': ['novnc', 'xvpvnc', 'spice-html5', 'spice-direct',
                             'serial', 'webmks'],
                },
            },
            'required': ['protocol', 'type'],
            'additionalProperties': False,
        },
    },
    'required': ['remote_console'],
    'additionalProperties': False,
}

get_vnc_console_response = {
    'type': 'object',
    'properties': {
        'console': {
            'type': 'object',
            'properties': {
                'type': {
                    'type': 'string',
                    'enum': ['novnc', 'xvpvnc'],
                    'description': '',
                },
                'url': {
                    'type': 'string',
                    'format': 'uri',
                    'description': '',
                },
            },
            'required': ['type', 'url'],
            'additionalProperties': False,
        },
    },
    'required': ['console'],
    'additionalProperties': False,
}

get_spice_console_response = {
    'type': 'object',
    'properties': {
        'console': {
            'type': 'object',
            'properties': {
                'type': {
                    'type': 'string',
                    'enum': ['spice-html5'],
                    'description': '',
                },
                'url': {
                    'type': 'string',
                    'format': 'uri',
                    'description': '',
                },
            },
            'required': ['type', 'url'],
            'additionalProperties': False,
        },
    },
    'required': ['console'],
    'additionalProperties': False,
}

get_serial_console_response = {
    'type': 'object',
    'properties': {
        'console': {
            'type': 'object',
            'properties': {
                'type': {
                    'type': 'string',
                    'enum': ['serial'],
                    'description': '',
                },
                'url': {
                    'type': 'string',
                    'format': 'uri',
                    'description': '',
                },
            },
            'required': ['type', 'url'],
            'additionalProperties': False,
        },
    },
    'required': ['console'],
    'additionalProperties': False,
}
