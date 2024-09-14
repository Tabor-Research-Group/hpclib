import os, getpass
from mdsrv.mdsrv import *

def main(username):
    args = parse_args()
    app_config( args.configure )
    DATA_DIRS = app.config.get( "DATA_DIRS", {} )
    DATA_DIRS.update( {
        "scratch": f"/scratch/user/{username}",
        "home": f"/home/{username}"
    } )
    app.config[ "DATA_DIRS" ] = DATA_DIRS
    def on_bind( host, port ):
        app.config.BROWSER_OPENED = True
        # open_browser( app, host, port, args.structure, args.trajectory, args.deltaTime, args.timeOffset, args.script )
    patch_socket_bind( on_bind )
    app.run(
        debug=app.config.get( 'DEBUG', False ),
        host=app.config.get( 'HOST', args.host ),
        port=app.config.get( 'PORT', args.port ),
        threaded=True,
        processes=1
    )

if __name__ == "__main__":
    username = getpass.getuser()
    print("serving from ", {
        "scratch": f"/scratch/user/{username}",
        "home": f"/home/{username}"
    })
    main(username)