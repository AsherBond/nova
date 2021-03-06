{
    "server": {
        "addresses": {
            "private": [
                {
                    "addr": "%(ip)s",
                    "version": 4,
                    "mac_addr": "aa:bb:cc:dd:ee:ff",
                    "type": "fixed"
                }
            ]
        },
        "adminPass": "%(password)s",
        "created": "%(isotime)s",
        "flavor": {
            "id": "1",
            "links": [
                {
                    "href": "%(host)s/flavors/1",
                    "rel": "bookmark"
                }
            ]
        },
        "host_id": "%(hostid)s",
        "id": "%(uuid)s",
        "image": {
            "id": "%(image_id)s",
            "links": [
                {
                    "href": "%(glance_host)s/images/%(image_id)s",
                    "rel": "bookmark"
                }
            ]
        },
        "links": [
            {
                "href": "%(host)s/v3/servers/%(uuid)s",
                "rel": "self"
            },
            {
                "href": "%(host)s/servers/%(uuid)s",
                "rel": "bookmark"
            }
        ],
        "metadata": {
            "meta_var": "meta_val"
        },
        "name": "new-server-test",
        "progress": 0,
        "status": "ACTIVE",
        "tenant_id": "openstack",
        "updated": "%(isotime)s",
        "user_id": "fake",
        "os-access-ips:access_ip_v4": "%(access_ip_v4)s",
        "os-access-ips:access_ip_v6": "%(access_ip_v6)s"
    }
}
