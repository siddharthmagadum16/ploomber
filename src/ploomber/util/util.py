from pathlib import Path
import importlib
from functools import wraps, reduce
import base64
# from glob import glob
# from pathlib import Path
# from collections import defaultdict
import shutil
import inspect
from itertools import chain
from glob import iglob

# from ploomber.products import File
from ploomber.exceptions import CallbackSignatureError, TaskRenderError


def requires(pkgs, name=None):
    """
    Check if packages were imported, raise ImportError with an appropriate
    message for missing ones
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            missing = [pkg for pkg in pkgs if importlib.util.find_spec(pkg)
                       is None]

            if missing:
                msg = reduce(lambda x, y: x+' '+y, missing)
                fn_name = name or f.__name__

                raise ImportError('{} {} required to use {}. Install {} by '
                                  'running "pip install {}"'
                                  .format(msg,
                                          'is' if len(missing) == 1 else 'are',
                                          fn_name,
                                          'it' if len(
                                              missing) == 1 else 'them',
                                          msg))

            return f(*args, **kwargs)

        return wrapper

    return decorator


@requires(['matplotlib'])
def path2fig(path_to_image, dpi=50):
    # FIXME: having this import at the top causes trouble with the
    # multiprocessing library, moving it here solves the problem but we
    # have to find a better solution.
    # more info: https://stackoverflow.com/q/16254191/709975
    import matplotlib.pyplot as plt

    data = plt.imread(path_to_image)
    height, width, _ = data.shape
    fig = plt.figure()
    fig.set_size_inches((width / dpi, height / dpi))
    ax = plt.Axes(fig, [0, 0, 1, 1])
    ax.set_axis_off()
    fig.add_axes(ax)
    ax.imshow(data)

    return fig


def safe_remove(path):
    if path.exists():
        if path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def image_bytes2html(data):
    fig_base64 = base64.encodebytes(data)
    img = fig_base64.decode("utf-8")
    html = '<img src="data:image/png;base64,' + img + '"></img>'
    return html


# TODO: finish or remove this
# def clean_up_files(dag, interactive=True):
#     """

#     * Get all files generated by the dag
#     * Find the set of parent directories
#     * The parents should only have the files that are generated by tge DAG

#     """
#     # WIP
#     # get products that generate Files
#     paths = [Path(str(t.product)) for t in dag.values()
#              if isinstance(t.product, File)]
#     # each file generates a .source file, also add it
#     paths = [(p, Path(str(p) + '.source')) for p in paths]
#     # flatten list
#     paths = [p for tup in paths for p in tup]

#     # get parents
#     parents = set([p.parent for p in paths])

#     # map parents to its files
#     parents_map = defaultdict(lambda: [])

#     for p in paths:
#         parents_map[str(p.parent)].append(str(p))

#     extra_all = []

#     # for every parent, find the extra files
#     for p in parents:
#         existing = glob(str(p) + '/*')
#         products = parents_map[str(p)]

#         extra = set(existing) - set(products)
#         extra_all.extend(list(extra))

#     for p in extra_all:
#         if interactive:
#             answer = input('Delete {} ? (y/n)'.format(p))

#             if answer == 'y':
#                 safe_remove(p)
#                 print('Deleted {}'.format(p))


def isiterable(obj):
    try:
        iter(obj)
    except TypeError:
        return False
    else:
        return True


# TODO: add more context to errors, which task and which hook?
def callback_check(fn, available, allow_default=True):
    """
    Check if a callback function signature requests available parameters

    Parameters
    ----------
    fn : callable
        Callable (e.g. a function) to check

    available : dict
        All available params

    allow_default : bool, optional
        Whether allow arguments with default values in "fn" or not

    Returns
    -------
    dict
        Dictionary with requested parameters
    """
    parameters = inspect.signature(fn).parameters
    optional = {name for name, param in parameters.items()
                if param.default != inspect._empty}
    # not all functions have __name__ (e.g. partials)
    fn_name = getattr(fn, '__name__', fn)

    if optional and not allow_default:
        raise CallbackSignatureError('Callback functions cannot have '
                                     'parameters with default values, '
                                     'got: {} in "{}"'.format(optional,
                                                              fn_name))

    required = {name for name, param in parameters.items()
                if param.default == inspect._empty}

    available_set = set(available)
    extra = required - available_set

    if extra:
        raise CallbackSignatureError('Callback function "{}" unknown '
                                     'parameter(s): {}, available ones are: '
                                     '{}'.format(fn_name, extra,
                                                 available_set))

    return {k: v for k, v in available.items() if k in required}


def signature_check(fn, params, task_name):
    """
    Verify if the function signature used as source in a PythonCallable
    task matches available params
    """
    params = set(params)
    parameters = inspect.signature(fn).parameters
    required = {name for name, param in parameters.items()
                if param.default == inspect._empty}

    extra = params - set(parameters.keys())
    missing = set(required) - params

    errors = []

    if extra:
        msg = ('The following params are not part of the function '
               'signature: {}'.format(extra))
        errors.append(msg)

    if missing:
        msg = 'The following params are missing: {}'.format(missing)
        errors.append(msg)

    if extra or missing:
        msg = '. '.join(errors)
        # not all functions have __name__ (e.g. partials)
        fn_name = getattr(fn, '__name__', fn)
        raise TaskRenderError('Error rendering task "{}" initialized with '
                              'function "{}". {}'
                              .format(task_name, fn_name, msg))

    return True


def _parse_module(dotted_path):
    parts = dotted_path.split('.')

    if len(parts) < 2:
        raise ImportError('Invalid module name, must be a dot separated '
                          'string, with at least '
                          '[module_name].[function_name]')

    return '.'.join(parts[:-1]), parts[-1]


def _load_factory(dotted_path):
    mod, name = _parse_module(dotted_path)

    try:
        module = importlib.import_module(mod)
    except ImportError as e:
        raise ImportError('An error happened when trying to '
                          'import module "{}"'.format(mod)) from e

    try:
        factory = getattr(module, name)
    except AttributeError as e:
        raise AttributeError('Could not get attribute "{}" from module '
                             '"{}", make sure it is a valid callable'
                             .format(name, mod)) from e

    return factory


def find_file_recursively(name, max_levels_up=6):
    """
    Find environment by looking into the current folder and parent folders,
    returns None if no file was found otherwise pathlib.Path to the file
    """
    def levels_up(n):
        return chain.from_iterable(iglob('../' * i + '**')
                                   for i in range(n + 1))

    path_to_file = None

    for filename in levels_up(max_levels_up):
        p = Path(filename)

        if p.name == name:
            path_to_file = filename
            break

    return path_to_file
